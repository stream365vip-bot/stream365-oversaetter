"""Trin C2 (8.11.2026): intern adresse til Fangarr.

En lille webserver i sin egen tråd ved siden af løkken. Den lytter på API_PORT inde i containeren og nås kun fra
Docker-netværket (ingen Traefik, ingen DNS, ingen åben port på serveren). Alle kald undtagen /api/health kræver
headeren X-Api-Token med værdien API_TOKEN.

Kun læsning: databasen åbnes skrivebeskyttet for hvert kald (som status-jobbet i trin B11), så webserveren aldrig
skriver i den og aldrig blokerer oversættelsen. Knapper (POST) kommer i en senere fase.

- GET /api/health  -> {"ok": true}
- GET /api/status  -> tilstand, aktuel titel, kø, pause, månedens forbrug og loft
- GET /api/items   -> titler fra databasen (?status=faerdig,fejlet&limit=100&days=30)
- GET /api/spend   -> forbrug pr. dag og type (?days=30) + månedens forbrug og loft
- GET /api/config  -> indstillinger uden hemmeligheder
- GET /api/reveal  -> trin C3: filer skjult for kunderne (venter) og de senest viste (?days=7)

Trin C4 (knapperne i Fangarr) - de eneste kald, der ændrer noget, og kun i Oversætterens egen database:
- GET  /api/estimate?series_id=12 | ?movie_id=7 -> afsnit pr. sæson (dansk / i gang / mangler) og ca. pris
- POST /api/translate   {"series_id": 12, "seasons": [1, 2]} | {"movie_id": 7}  -> "Oversæt nu"
- POST /api/items/retry {"key": "episode:345"}  -> "Prøv igen"
- POST /api/items/skip  {"key": "episode:345"}  -> "Opgiv"
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import knap
from .clients import ApiError, Bazarr, Radarr, Sonarr
from .config import Config
from .db import Database, month_key

log = logging.getLogger("oversaetter")

ITEM_FIELDS = (
    "key",
    "media_type",
    "media_id",
    "series_id",
    "title",
    "status",
    "attempts",
    "reason",
    "source",
    "started_at",
    "updated_at",
    "cost_usd",
    "flags_before",
    "flags_after",
    "fixes",
    "en_hi",
    "plex_refresh",
    "bazarr_sync",
    "dk_providers",
    "wait_started_at",
    "video_path",
)
MAX_LIMIT = 1000
MAX_BODY = 64 * 1024
MAX_DAYS = 400


class Api:
    """Svarene bygges her, så de kan testes uden en rigtig webserver."""

    def __init__(self, cfg: Config, runner=None, clock=None):
        self.cfg = cfg
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(UTC))
        self._status_job = None
        self._clients = None
        self._write_lock = threading.Lock()  # knapperne skriver én ad gangen

    # --- database ---------------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection | None:
        path = os.path.join(self.cfg.data_dir, "oversaetter.db")
        if not os.path.exists(path):
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _month_usd(self, conn: sqlite3.Connection) -> float:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE month = ?", (month_key(),)).fetchone()
        return round(float(row[0]), 4)

    # --- svar --------------------------------------------------------------------------------
    def status(self) -> dict:
        if self._status_job is None:
            from .status import StatusJob

            self._status_job = StatusJob(self.cfg, self.runner)
        out = self._status_job.translator()
        out["no_english"] = out.pop("_no_english", 0)
        r = self.runner
        last = getattr(r, "last_round", None) if r is not None else None
        out["last_round"] = last.isoformat(timespec="seconds") if last else None
        out["poll_minutes"] = self.cfg.poll_minutes
        out["new_warning"] = getattr(r, "new_warning", None) if r is not None else None
        out["usd_to_dkk"] = self.cfg.usd_to_dkk
        return out

    def items(self, query: dict[str, str]) -> dict:
        statuses = [s.strip() for s in query.get("status", "").split(",") if s.strip()]
        limit = _int(query.get("limit"), 100, 1, MAX_LIMIT)
        days = _int(query.get("days"), 0, 0, MAX_DAYS)
        sql = f"SELECT {', '.join(ITEM_FIELDS)} FROM items"
        where, args = [], []
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            args += statuses
        if days:
            where.append("updated_at >= ?")
            args.append((self.clock() - timedelta(days=days)).isoformat(timespec="seconds"))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        conn = self._db()
        if conn is None:
            return {"items": [], "counts": {}}
        try:
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
        finally:
            conn.close()
        for r in rows:
            if r.get("video_path"):
                r["file"] = os.path.basename(r.pop("video_path"))
            else:
                r.pop("video_path", None)
            if r.get("dk_providers"):
                try:
                    r["dk_providers"] = json.loads(r["dk_providers"])
                except ValueError:
                    pass
        return {"items": rows, "counts": counts}

    def spend(self, query: dict[str, str]) -> dict:
        days = _int(query.get("days"), 30, 1, MAX_DAYS)
        since = (self.clock() - timedelta(days=days)).strftime("%Y-%m-%d")
        out = {"days": [], "month_usd": 0.0, "budget_usd": round(self.cfg.monthly_budget_usd, 2)}
        conn = self._db()
        if conn is None:
            return out
        try:
            rows = conn.execute(
                "SELECT substr(ts, 1, 10) AS day, substr(item_key, 1, instr(item_key, ':') - 1) AS type, "
                "COUNT(DISTINCT item_key) AS titles, SUM(cost_usd) AS usd FROM spend "
                "WHERE substr(ts, 1, 10) >= ? AND item_key LIKE '%:%' GROUP BY day, type ORDER BY day",
                (since,),
            ).fetchall()
            out["days"] = [
                {"day": r["day"], "type": r["type"], "titles": r["titles"], "usd": round(r["usd"], 4)} for r in rows
            ]
            out["month_usd"] = self._month_usd(conn)
        finally:
            conn.close()
        return out

    def config(self) -> dict:
        c = self.cfg
        return {
            "translate_mode": c.translate_mode,
            "translate_model": c.translate_model,
            "translate_effort": c.translate_effort,
            "fallback_model": c.fallback_model,
            "batch_api": c.batch_api,
            "monthly_budget_usd": c.monthly_budget_usd,
            "usd_to_dkk": c.usd_to_dkk,
            "dry_run": c.dry_run,
            "poll_minutes": c.poll_minutes,
            "provider_wait": c.provider_wait,
            "provider_wait_days_movies": c.provider_wait_days_movies,
            "provider_wait_days_series": c.provider_wait_days_series,
            "translate_all_since": c.translate_all_since,
            "new_lookback_days": c.new_lookback_days,
            "bazarr_profile": c.bazarr_profile,
            "translate_tag": c.translate_tag,
            "plex_refresh": bool(c.plex_url),
            "sync_after_upload": c.sync_after_upload,
            "max_searches_per_round": c.max_searches_per_round,
            "reveal": c.reveal,
            "reveal_safety_hours": c.reveal_safety_hours,
        }

    def reveal(self, query: dict[str, str]) -> dict:
        days = _int(query.get("days"), 7, 1, 30)
        now = self.clock()
        safety = self.cfg.reveal_safety_hours
        r = getattr(self.runner, "revealer", None) if self.runner is not None else None
        last = getattr(r, "last_run", None)
        out = {
            "enabled": self.cfg.reveal,
            "safety_hours": safety,
            "interval_seconds": self.cfg.reveal_interval_seconds,
            "last_run": last.isoformat(timespec="seconds") if last else None,
            "last_error": getattr(r, "last_error", None),
            "waiting": [],
            "shown": [],
        }
        conn = self._db()
        if conn is None:
            return out
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reveal'").fetchone():
                return out
            fields = "key, import_at, media_type, media_id, title, path, status, reason, danish, shown_at, shown_by"
            waiting = conn.execute(f"SELECT {fields} FROM reveal WHERE status = 'venter' ORDER BY import_at").fetchall()
            since = (now - timedelta(days=days)).isoformat(timespec="seconds")
            shown = conn.execute(
                f"SELECT {fields} FROM reveal WHERE status = 'vist' AND shown_at >= ? ORDER BY shown_at DESC LIMIT 500",
                (since,),
            ).fetchall()
            states = {
                row["key"]: (row["status"], row["reason"])
                for row in conn.execute(
                    f"SELECT key, status, reason FROM items WHERE key IN ({', '.join('?' for _ in waiting) or 'NULL'})",
                    [w["key"] for w in waiting],
                ).fetchall()
            }
        finally:
            conn.close()

        def clean(row) -> dict:
            d = dict(row)
            d["file"] = os.path.basename(d.pop("path") or "")
            return d

        for w in waiting:
            d = clean(w)
            try:
                at = datetime.fromisoformat(w["import_at"])
                d["age_hours"] = round((now - at).total_seconds() / 3600, 2)
                d["reveal_by"] = (at + timedelta(hours=safety)).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                d["age_hours"], d["reveal_by"] = None, None
            d["translation"], d["translation_reason"] = states.get(w["key"], (None, None))
            out["waiting"].append(d)
        out["shown"] = [clean(x) for x in shown]
        return out

    # --- trin C4: knapperne ------------------------------------------------------------------------
    def clients(self):
        if self._clients is None:
            import httpx2 as httpx

            from .clients import GET_TIMEOUT

            http = httpx.Client(timeout=httpx.Timeout(GET_TIMEOUT))
            self._clients = (Sonarr(self.cfg, http), Radarr(self.cfg, http), Bazarr(self.cfg, http))
        return self._clients

    def _rw(self) -> Database:
        return Database(os.path.join(self.cfg.data_dir, "oversaetter.db"))

    def estimate(self, query: dict[str, str]) -> tuple[int, dict]:
        sonarr, radarr, bazarr = self.clients()
        db = self._rw()
        try:
            if query.get("series_id", "").isdigit():
                return 200, knap.series_overview(self.cfg, db, sonarr, bazarr, int(query["series_id"]))
            if query.get("movie_id", "").isdigit():
                return 200, knap.movie_overview(self.cfg, db, radarr, bazarr, int(query["movie_id"]))
            return 400, {"error": "angiv series_id eller movie_id"}
        finally:
            db.conn.close()

    def _wake(self) -> None:
        wake = getattr(self.runner, "wake", None) if self.runner is not None else None
        if wake is not None:
            wake.set()

    def handle_post(self, path: str, body: dict) -> tuple[int, dict]:
        if path not in ("/api/translate", "/api/items/retry", "/api/items/skip"):
            return 404, {"error": "findes ikke"}
        sonarr, radarr, bazarr = self.clients()
        with self._write_lock:
            db = self._rw()
            try:
                if path == "/api/translate":
                    if isinstance(body.get("movie_id"), int):
                        out = knap.translate_movie(db, radarr, bazarr, body["movie_id"])
                    elif isinstance(body.get("series_id"), int) and isinstance(body.get("seasons"), list):
                        seasons = [int(x) for x in body["seasons"] if isinstance(x, int)]
                        if not seasons:
                            return 400, {"error": "vælg mindst én sæson"}
                        out = knap.translate_series(db, sonarr, bazarr, body["series_id"], seasons)
                    else:
                        return 400, {"error": "angiv movie_id eller series_id og seasons"}
                    log.info("Knap: Oversæt nu - %s: %d film/afsnit lagt i kø", out["title"], out["added"])
                else:
                    key = str(body.get("key") or "")
                    if path == "/api/items/retry":
                        out = knap.retry(db, key)
                        log.info("Knap: Prøv igen - %s (%s) behandles forfra i næste runde", out["title"], key)
                    else:
                        out = knap.give_up(db, key)
                        log.info("Knap: Opgiv - %s (%s) vælges ikke igen", out["title"], key)
            except knap.Conflict as e:
                return 409, {"error": str(e)}
            except ValueError as e:
                return 400, {"error": str(e)}
            finally:
                db.conn.close()
        self._wake()
        return 200, out

    def handle(self, path: str, query: dict[str, str]) -> tuple[int, dict]:
        if path == "/api/estimate":
            try:
                return self.estimate(query)
            except ApiError as e:
                return 502, {"error": str(e)}
        routes = {
            "/api/status": lambda: self.status(),
            "/api/items": lambda: self.items(query),
            "/api/spend": lambda: self.spend(query),
            "/api/config": lambda: self.config(),
            "/api/reveal": lambda: self.reveal(query),
        }
        if path not in routes:
            return 404, {"error": "findes ikke"}
        return 200, routes[path]()


def _int(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw))) if raw not in (None, "") else default
    except ValueError:
        return default


def make_handler(api: Api, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "oversaetter"
        sys_version = ""

        def log_message(self, *args) -> None:  # ingen adgangslog (Fangarr spørger ofte)
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (navnet kræves af http.server)
            url = urlparse(self.path)
            if url.path == "/api/health":
                return self._send(200, {"ok": True})
            given = self.headers.get("X-Api-Token", "")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return self._send(401, {"error": "forkert eller manglende X-Api-Token"})
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                code, body = api.handle(url.path, query)
            except Exception as e:  # svaret må aldrig vælte tråden
                log.warning("API: fejl ved %s (%s: %s)", url.path, type(e).__name__, e)
                code, body = 500, {"error": f"{type(e).__name__}"}
            self._send(code, body)

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            given = self.headers.get("X-Api-Token", "")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return self._send(401, {"error": "forkert eller manglende X-Api-Token"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                return self._send(413, {"error": "for stort kald"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "ugyldig JSON"})
            try:
                code, out = api.handle_post(url.path, body)
            except ApiError as e:
                code, out = 502, {"error": str(e)}
            except Exception as e:
                log.warning("API: fejl ved %s (%s: %s)", url.path, type(e).__name__, e)
                code, out = 500, {"error": f"{type(e).__name__}"}
            self._send(code, out)

    return Handler


def start(cfg: Config, runner) -> ThreadingHTTPServer | None:
    """Start den interne adresse, hvis API_PORT er sat. Fejler aldrig (driften fortsætter uden)."""
    if not cfg.api_port:
        log.info("API: API_PORT er 0 - den interne adresse til Fangarr er slået fra")
        return None
    if not cfg.api_token:
        log.warning("API: API_PORT er sat, men API_TOKEN er tom - den interne adresse startes ikke")
        return None
    try:
        server = ThreadingHTTPServer(("0.0.0.0", cfg.api_port), make_handler(Api(cfg, runner), cfg.api_token))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="api", daemon=True).start()
        if runner is not None and getattr(runner, "wake", None) is None:
            runner.wake = threading.Event()
        log.info("API: intern adresse til Fangarr lytter på port %d (kræver X-Api-Token)", cfg.api_port)
        return server
    except Exception as e:
        log.warning("API: kunne ikke starte den interne adresse (%s: %s) - driften fortsætter", type(e).__name__, e)
        return None
