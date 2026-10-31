"""Trin B11: serverstatus til appens backend (STATUS_URL), hvert 5. minut.

Jobbet kører i sin egen tråd, uafhængigt af driften: det læser kun (disk, /proc, Radarr, Sonarr, Seerr, Bazarr,
qBittorrent, en docker-socket-proxy, Plex og vores egen database) og sender ét POST med de felter, der kunne læses.
Et felt, der ikke kan læses, udelades, og årsagen logges én gang (og igen, når den ændrer sig, eller når feltet virker
igen).

Det må aldrig påvirke oversættelsen: alle fejl fanges her, hvert kald har en hård grænse på 10 sekunder, alle
målinger køres samtidig med en fælles frist, og hele jobbet tager højst 60 sekunder. Databasen åbnes skrivebeskyttet
på sin egen forbindelse, og runneren læses kun (ingen låse, ingen ændringer). Tokenet logges aldrig.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable

import httpx2 as httpx

from .config import Config, read_key
from .db import month_key
from .deadline import call_with_timeout

log = logging.getLogger("oversaetter")

INTERVAL = 5 * 60  # sekunder mellem to statusbeskeder
CALL_TIMEOUT = 10.0  # højst så mange sekunder pr. kald
MOUNT_TIMEOUT = 5.0  # mount_ok: en kendt fil under /mnt/unionfs/Media skal kunne læses inden for 5 sekunder
COLLECT_DEADLINE = 45.0  # alle målinger (samtidig) skal være færdige inden da; resten udelades
JOB_LIMIT = 60.0  # hele jobbet, inkl. POST
TOKEN_HEADER = "X-Oversaetter-Token"
GB = 1024**3
QUEUE_PAGE = 500  # så mange køposter hentes for at tælle failed_downloads
FAILED = ("warning", "failed")  # queue-poster med denne status (eller trackedDownloadStatus) tæller som fejlet


class Skip(Exception):
    """Feltet udelades. Teksten er årsagen til loggen (dansk, uden nøgler). info=True: ikke sat op (INFO)."""

    def __init__(self, reason: str, info: bool = False):
        super().__init__(reason)
        self.info = info


def _gb(n: float) -> float:
    return round(n / GB, 1)


def _http_reason(e: Exception) -> str:
    if isinstance(e, TimeoutError):
        return f"intet svar inden for {CALL_TIMEOUT:g} sekunder"
    if isinstance(e, httpx.TimeoutException):
        return f"intet svar inden for {CALL_TIMEOUT:g} sekunder (timeout)"
    if isinstance(e, httpx.HTTPError):
        return f"kunne ikke forbinde ({type(e).__name__})"
    return str(e) or type(e).__name__


class StatusJob:
    """Saml og send serverstatus. Alle afhængigheder kan udskiftes i testene."""

    def __init__(
        self,
        cfg: Config,
        runner=None,
        http: httpx.Client | None = None,
        proc_dir: str = "/proc",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ):
        self.cfg = cfg
        self.runner = runner
        self.http = http or httpx.Client(timeout=httpx.Timeout(CALL_TIMEOUT))
        self.proc_dir = proc_dir
        self.clock = clock
        self.stopping = threading.Event()
        self.sleep = sleep or self.stopping.wait
        self.reasons: dict[str, str] = {}  # felt -> seneste loggede årsag (så den kun logges én gang)
        self.busy: dict[str, threading.Thread] = {}  # målinger, der stadig hænger fra en tidligere runde
        self.cpu_prev: tuple[int, int] | None = None  # (i alt, ledig) fra /proc/stat ved seneste måling
        self.sent_once = False

    # --- log én gang --------------------------------------------------------------------------
    def _note(self, name: str, reason: str, info: bool = False) -> None:
        if self.reasons.get(name) == reason:
            return
        self.reasons[name] = reason
        (log.info if info else log.warning)("Status: %s udelades - %s", name, reason)

    def _ok(self, name: str) -> None:
        if self.reasons.pop(name, None) is not None:
            log.info("Status: %s virker igen", name)

    # --- disk og mount ------------------------------------------------------------------------
    def disk(self) -> dict:
        return {"disk_free_gb": _gb(shutil.disk_usage(self.cfg.status_disk_path).free)}

    def unionfs(self) -> dict:
        return {"unionfs_free_gb": _gb(shutil.disk_usage(self.cfg.status_unionfs_path).free)}

    def mount(self) -> dict:
        """mount_ok: en kendt fil (STATUS_MOUNT_FILE) eller blot mappen /mnt/unionfs/Media kan læses inden for 5 s."""
        path = self.cfg.status_mount_file or os.path.join(self.cfg.status_unionfs_path, "Media")

        def read():
            if os.path.isdir(path):
                return bool(os.listdir(path))
            with open(path, "rb") as f:
                f.read(4096)
            return True

        try:
            return {"mount_ok": bool(call_with_timeout(read, MOUNT_TIMEOUT))}
        except TimeoutError:
            log.debug("Status: %s kunne ikke læses inden for %g s", path, MOUNT_TIMEOUT)
        except OSError as e:
            log.debug("Status: %s kunne ikke læses (%s)", path, e)
        return {"mount_ok": False}

    # --- /proc ------------------------------------------------------------------------------------
    def _proc(self, name: str) -> str:
        with open(os.path.join(self.proc_dir, name), encoding="ascii") as f:
            return f.read()

    def _cpu_times(self) -> tuple[int, int]:
        parts = [int(x) for x in self._proc("stat").splitlines()[0].split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
        return sum(parts[:8]), idle

    def system(self) -> dict:
        out: dict = {}
        prev = self.cpu_prev
        if prev is None:  # første måling: gennemsnit over 1 sekund
            prev = self._cpu_times()
            time.sleep(1.0)
        total, idle = self._cpu_times()
        self.cpu_prev = (total, idle)
        if total > prev[0]:
            out["cpu_pct"] = round(100.0 * (1 - (idle - prev[1]) / (total - prev[0])), 1)
        mem = {}
        for line in self._proc("meminfo").splitlines():
            key, _, rest = line.partition(":")
            if rest.strip():
                mem[key] = int(rest.split()[0])
        if mem.get("MemTotal") and "MemAvailable" in mem:
            out["ram_pct"] = round(100.0 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
        out["load"] = float(self._proc("loadavg").split()[0])
        out["uptime_days"] = round(float(self._proc("uptime").split()[0]) / 86400, 1)
        return out

    # --- Docker -----------------------------------------------------------------------------------
    def containers(self) -> dict:
        """Kun via en docker-socket-proxy (STATUS_DOCKER=http://...). Docker-socket'en monteres aldrig i containeren."""
        base = self.cfg.status_docker.strip().rstrip("/")
        if not base:
            raise Skip("STATUS_DOCKER er tom (ingen docker-socket-proxy sat op)", info=True)
        if not base.startswith(("http://", "https://")):
            raise Skip(f"STATUS_DOCKER={base} er ikke en http-adresse til en docker-socket-proxy", info=True)
        wanted = [n.strip() for n in self.cfg.status_containers.split(",") if n.strip()]
        if not wanted:
            raise Skip("STATUS_CONTAINERS er tom", info=True)
        r = self.http.get(base + "/containers/json", params={"all": "true"}, timeout=CALL_TIMEOUT)
        if r.status_code >= 400:
            raise Skip(f"Docker svarede med HTTP {r.status_code}")
        running = set()
        for c in r.json():
            if c.get("State") == "running":
                running.update(n.lstrip("/") for n in c.get("Names") or [])
        return {"containers_down": [n for n in wanted if n not in running]}

    # --- tjenester ----------------------------------------------------------------------------------
    def _get(self, name: str, url: str, path: str, params=None, headers=None):
        r = self.http.get(url + path, params=params, headers=headers or {}, timeout=CALL_TIMEOUT)
        if r.status_code >= 400:
            hint = " (forkert API-nøgle?)" if r.status_code in (401, 403) else ""
            raise Skip(f"{name} svarede med HTTP {r.status_code} på {path}{hint}")
        try:
            return r.json()
        except ValueError:
            raise Skip(f"{name} svarede ikke med gyldige data på {path}") from None

    def _queue(self, name: str, url: str, key: str, unknown: str) -> tuple[int, int]:
        if not url or not key:
            raise Skip(f"{name.upper()}_URL eller {name.upper()}_API_KEY mangler", info=True)
        data = self._get(
            name.capitalize(),
            url,
            "/api/v3/queue",
            {"page": 1, "pageSize": QUEUE_PAGE, unknown: "true"},
            {"X-Api-Key": key},
        )
        records = data.get("records") or []
        failed = sum(
            1
            for rec in records
            if str(rec.get("status", "")).lower() in FAILED
            or str(rec.get("trackedDownloadStatus", "")).lower() in ("warning", "error")
        )
        return int(data.get("totalRecords", len(records))), failed

    def radarr(self) -> dict:
        total, failed = self._queue("radarr", self.cfg.radarr_url, self.cfg.radarr_api_key, "includeUnknownMovieItems")
        return {"radarr_queue": total, "_failed_radarr": failed}

    def sonarr(self) -> dict:
        total, failed = self._queue("sonarr", self.cfg.sonarr_url, self.cfg.sonarr_api_key, "includeUnknownSeriesItems")
        return {"sonarr_queue": total, "_failed_sonarr": failed}

    def qbittorrent(self) -> dict:
        url = self.cfg.qbittorrent_url.rstrip("/")
        secret = read_key(self.cfg.qbittorrent_file)
        if not url or ":" not in secret:
            raise Skip(f"QBITTORRENT_URL eller {self.cfg.qbittorrent_file} (bruger:kode) er ikke sat", info=True)
        user, _, password = secret.partition(":")
        headers = {"Referer": url, "Origin": url}  # qBittorrents CSRF-tjek
        r = self.http.post(
            url + "/api/v2/auth/login",
            data={"username": user, "password": password},
            headers=headers,
            timeout=CALL_TIMEOUT,
        )
        if r.status_code == 403:
            raise Skip("qBittorrent afviser login (HTTP 403 - for mange forkerte forsøg, IP'en er spærret)")
        if r.status_code >= 400 or r.text.strip().lower().startswith("fails"):
            raise Skip(f"qBittorrent afviser bruger/kode i {self.cfg.qbittorrent_file}")
        cookie = "; ".join(f"{k}={v}" for k, v in r.cookies.items())
        info = self._get("qBittorrent", url, "/api/v2/transfer/info", headers={**headers, "Cookie": cookie})
        return {"download_speed_mbps": round(float(info.get("dl_info_speed", 0)) * 8 / 1_000_000, 1)}

    def seerr(self) -> dict:
        if not self.cfg.seerr_url or not self.cfg.seerr_api_key:
            raise Skip("SEERR_URL eller SEERR_API_KEY mangler", info=True)
        data = self._get(
            "Seerr",
            self.cfg.seerr_url,
            "/api/v1/request",
            {"take": 1, "skip": 0, "filter": "pending"},
            {"X-Api-Key": self.cfg.seerr_api_key},
        )
        return {"seerr_pending": int((data.get("pageInfo") or {}).get("results", 0))}

    def bazarr(self) -> dict:
        if not self.cfg.bazarr_url or not self.cfg.bazarr_api_key:
            raise Skip("BAZARR_URL eller BAZARR_API_KEY mangler", info=True)
        headers = {"X-API-KEY": self.cfg.bazarr_api_key}
        out = {}
        for field, path in (("bazarr_wanted_movies", "movies"), ("bazarr_wanted_episodes", "episodes")):
            data = self._get("Bazarr", self.cfg.bazarr_url, f"/api/{path}/wanted", {"start": 0, "length": 1}, headers)
            out[field] = int(data.get("total", len(data.get("data") or [])))
        return out

    def plex(self) -> dict:
        """Igangværende afspilninger direkte fra Plex (i stedet for Tautulli/Tracearr)."""
        if not self.cfg.status_plex:
            raise Skip("STATUS_PLEX=false", info=True)
        token = read_key(self.cfg.plex_token_file)
        if not self.cfg.plex_url or not token:
            raise Skip(f"PLEX_URL eller tokenet i {self.cfg.plex_token_file} mangler", info=True)
        data = self._get(
            "Plex",
            self.cfg.plex_url,
            "/status/sessions",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
        )
        sessions = (data.get("MediaContainer") or {}).get("Metadata") or []
        transcodes = sum(1 for s in sessions if (s.get("TranscodeSession") or {}).get("videoDecision") == "transcode")
        kbps = sum(int((s.get("Session") or {}).get("bandwidth") or 0) for s in sessions)
        return {
            "plex_streams": len(sessions),
            "plex_transcodes": transcodes,
            "plex_bandwidth_mbps": round(kbps / 1000, 1),
        }

    # --- oversætteren -----------------------------------------------------------------------------
    def translator(self) -> dict:
        cfg, r = self.cfg, self.runner
        out: dict = {"budget_usd": round(cfg.monthly_budget_usd, 2)}
        db_path = os.path.join(cfg.data_dir, "oversaetter.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                month = month_key()
                spent = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE month = ?", (month,))
                out["month_usd"] = round(float(spent.fetchone()[0]), 4)
                counts = dict(
                    conn.execute(
                        "SELECT status, COUNT(*) FROM items WHERE status IN ('venter_paa_dansk', 'mangler_engelsk') "
                        "GROUP BY status"
                    ).fetchall()
                )
                out["waiting_for_danish"] = counts.get("venter_paa_dansk", 0)
                out["_no_english"] = counts.get("mangler_engelsk", 0)
                failed = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE status = 'fejlet' AND substr(updated_at, 1, 7) = ?", (month,)
                )
                out["failed"] = int(failed.fetchone()[0])
            finally:
                conn.close()
        if r is not None:
            current = getattr(r, "current", None)
            pause_until = getattr(r, "pause_until", None)
            batch_since = getattr(r, "batch_since", None)
            if pause_until is not None and r.clock() < pause_until:
                out["state"] = "pause"
                if getattr(r, "pause_at", None) is not None:
                    out["pause_until"] = r.pause_at.isoformat(timespec="seconds")
            elif current is not None and batch_since is not None:
                out["state"] = "venter"
                out["batch_wait_min"] = int(max(0.0, time.time() - batch_since) // 60)
            elif current is not None:
                out["state"] = "i_gang"
            else:
                out["state"] = "ledig"
            if current is not None:
                out["title"] = current[1]
            out["queue"] = int(getattr(r, "queue", 0) or 0)
        return out

    # --- saml og send -------------------------------------------------------------------------------
    def collectors(self) -> dict[str, Callable[[], dict]]:
        return {
            "disk_free_gb": self.disk,
            "unionfs_free_gb": self.unionfs,
            "mount_ok": self.mount,
            "containers_down": self.containers,
            "cpu/ram/load/uptime": self.system,
            "radarr_queue": self.radarr,
            "sonarr_queue": self.sonarr,
            "download_speed_mbps": self.qbittorrent,
            "seerr_pending": self.seerr,
            "bazarr_wanted": self.bazarr,
            "plex": self.plex,
            "translator": self.translator,
        }

    def collect(self, deadline: float = COLLECT_DEADLINE) -> dict:
        """Kør alle målinger samtidig. En måling, der ikke er færdig inden fristen, udelades (og hænger videre)."""
        results: dict[str, dict | Exception] = {}
        threads: dict[str, threading.Thread] = {}
        for name, fn in self.collectors().items():
            old = self.busy.get(name)
            if old is not None and old.is_alive():
                self._note(name, "forrige måling hænger stadig")
                continue

            def run(name=name, fn=fn):
                try:
                    results[name] = fn()
                except BaseException as e:
                    results[name] = e

            t = threading.Thread(target=run, name=f"status-{name}", daemon=True)
            t.start()
            threads[name] = t
        end = self.clock() + deadline
        for t in threads.values():
            t.join(max(0.0, end - self.clock()))
        payload: dict = {}
        failed_downloads, have_failed = 0, False
        for name, t in threads.items():
            if t.is_alive():
                self.busy[name] = t
                self._note(name, f"intet svar inden for {deadline:g} sekunder")
                continue
            self.busy.pop(name, None)
            value = results.get(name)
            if isinstance(value, Skip):
                self._note(name, str(value), value.info)
                continue
            if isinstance(value, BaseException):
                self._note(name, _http_reason(value))
                continue
            self._ok(name)
            for key in ("_failed_radarr", "_failed_sonarr"):
                if key in value:
                    failed_downloads += value.pop(key)
                    have_failed = True
            if "_no_english" in value:
                payload["bazarr_no_english"] = value.pop("_no_english")
            if name == "translator":
                payload["translator"] = value
            else:
                payload.update(value)
        if have_failed:
            payload["failed_downloads"] = failed_downloads
        return payload

    def send(self, payload: dict) -> bool:
        """POST til STATUS_URL. Fejl bliver til én WARNING (og INFO, når det virker igen)."""
        token = read_key(self.cfg.feed_token_file)
        if not token:
            self._note("POST", f"intet token i {self.cfg.feed_token_file} - status sendes ikke")
            return False

        def post():
            return self.http.post(
                self.cfg.status_url, json=payload, headers={TOKEN_HEADER: token}, timeout=CALL_TIMEOUT
            ).status_code

        try:
            code = call_with_timeout(post, CALL_TIMEOUT)
        except Exception as e:
            self._note("POST", f"kunne ikke sende til {self.cfg.status_url} ({_http_reason(e)})")
            return False
        if code >= 300:
            hint = f" - tjek tokenet i {self.cfg.feed_token_file}" if code in (401, 403) else ""
            self._note("POST", f"backenden svarede med HTTP {code}{hint}")
            return False
        if self.reasons.pop("POST", None) is not None:
            log.info("Status: sendt igen til %s", self.cfg.status_url)
        elif not self.sent_once:
            log.info("Status: første statusbesked sendt til %s (%d felter)", self.cfg.status_url, len(payload))
        else:
            log.debug("Status: sendt (%d felter)", len(payload))
        self.sent_once = True
        return True

    def run_once(self) -> bool:
        """Én runde: saml og send. Fejler aldrig og tager højst JOB_LIMIT sekunder."""
        try:
            payload = call_with_timeout(self.collect, JOB_LIMIT - CALL_TIMEOUT - 2)
            return self.send(payload)
        except Exception as e:  # sikkerhedsnet: status-jobbet må aldrig vælte noget
            log.warning("Status: runden fejlede (%s: %s)", type(e).__name__, e)
            return False

    # --- tråden -------------------------------------------------------------------------------------
    def loop(self) -> None:
        while not self.stopping.is_set():
            started = self.clock()
            self.run_once()
            self.sleep(max(1.0, INTERVAL - (self.clock() - started)))

    def start(self) -> threading.Thread:
        log.info("Status: serverstatus sendes hvert %g. minut til %s", INTERVAL / 60, self.cfg.status_url)
        t = threading.Thread(target=self.loop, name="status-job", daemon=True)
        t.start()
        return t


def start(cfg: Config, runner) -> StatusJob | None:
    """Start status-jobbet i baggrunden, hvis STATUS_URL er sat. Fejler aldrig."""
    if not cfg.status_url:
        log.info("Status: STATUS_URL er tom - serverstatus sendes ikke")
        return None
    try:
        job = StatusJob(cfg, runner)
        job.start()
        return job
    except Exception as e:
        log.warning("Status: kunne ikke starte status-jobbet (%s: %s) - driften fortsætter", type(e).__name__, e)
        return None
