"""Trin C3 (8.11.2026): nye film og afsnit vises først for kunderne, når de har dansk tale eller dansk tekst.

Autoscans import-hændelser er slået fra i Sonarr/Radarr, så Plex ser ikke nye filer af sig selv. I stedet kører
denne del i sin egen tråd (hvert REVEAL_INTERVAL_SECONDS) og:

1. finder nye importer i Radarr/Sonarr-historikken (downloadFolderImported) siden trinnet blev slået til,
2. gemmer dem i tabellen reveal som "venter",
3. tjekker hver ventende fil: dansk originalsprog, dansk lydspor, indlejret dansk tekst eller en dansk tekstfil ved
   siden af videoen (fx vores egen oversættelse eller en tekst, Bazarr har hentet),
4. beder Plex scanne mappen, når ALLE ventende filer i mappen er klar. Plex scanner en hel mappe ad gangen, så et
   afsnit, der ikke er klar, holder resten af sæsonmappen tilbage.

Sikkerhedsnet: en fil, der har ventet REVEAL_SAFETY_HOURS, vises alligevel (shown_by = sikkerhedsnet). Fangarr
sender en Telegram-besked om hver af dem.

En fil, der er væk igen (opgraderet eller slettet), markeres som "erstattet". Den nye fil er en ny import og får
sin egen række. Fejl her må aldrig stoppe oversættelsen: alt fanges og skrives som advarsel.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx2 as httpx

from .clients import GET_TIMEOUT, ApiError, Radarr, Sonarr, parse_date
from .config import Config
from .db import Database, iso
from .plex import refresh_folder
from .selection import danish_on_disk
from .tjenester import DANISH_AUDIO, PROBE_TIMEOUT, audio_languages

log = logging.getLogger("oversaetter")

STARTED_KEY = "reveal_started"
KEEP_DAYS = 30  # viste og erstattede rækker slettes efter så mange dage
REASONS = {
    "original": "dansk originalsprog",
    "lyd": "dansk tale",
    "tekst": "indlejret dansk tekst",
    "fil": "dansk tekstfil",
}


def subtitle_languages(video: str, run_cmd=subprocess.run, timeout: float = PROBE_TIMEOUT) -> list[str] | None:
    """Sprogene på filens indlejrede tekstspor (ffprobe). None, hvis filen ikke kunne læses."""
    args = ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index:stream_tags=language"]
    try:
        out = run_cmd([*args, "-of", "json", video], capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        streams = json.loads((out.stdout or b"{}").decode("utf-8", "replace")).get("streams") or []
    except (ValueError, AttributeError):
        return None
    return [str((s.get("tags") or {}).get("language") or "").strip().lower() for s in streams]


def _danish_name(lang) -> bool:
    name = (lang or {}).get("name") if isinstance(lang, dict) else lang
    return str(name or "").strip().lower() in ("danish", "dansk")


def pending_in(db: Database, key: str, folder: str) -> bool:
    """Venter titlen - eller en anden fil i samme mappe - på at blive vist? (til oversætteren)"""
    row = db.conn.execute(
        "SELECT 1 FROM reveal WHERE status = 'venter' AND (key = ? OR folder = ?) LIMIT 1", (key, folder)
    ).fetchone()
    return row is not None


class Revealer:
    def __init__(self, cfg: Config, db: Database, http: httpx.Client | None = None, run_cmd=subprocess.run, now=None):
        self.cfg, self.db = cfg, db
        self.http = http or httpx.Client(timeout=httpx.Timeout(GET_TIMEOUT))
        self.sonarr, self.radarr = Sonarr(cfg, self.http), Radarr(cfg, self.http)
        self.run_cmd = run_cmd
        self.now = now or (lambda: datetime.now(UTC))
        self.wake = threading.Event()  # sættes af oversætteren, når en tekst er færdig (så vises den hurtigt)
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self._warned_plex = False

    # --- start-tidspunkt -------------------------------------------------------------------------
    def started(self) -> datetime:
        """Første gang trinnet kørte. Importer fra før er allerede synlige (Autoscan) og røres ikke."""
        hit = self.db.lookup(STARTED_KEY)
        if hit is not None and hit[0]:
            when = parse_date(json.loads(hit[0]))
            if when is not None:
                return when
        when = self.now()
        self.db.save_lookup(STARTED_KEY, json.dumps(iso(when)))
        log.info("Udgivelse: slået til - nye filer fra nu af vises først i Plex, når de har dansk tale eller tekst")
        return when

    # --- 1-2: nye importer -----------------------------------------------------------------------------
    def collect(self) -> int:
        start = max(self.started(), self.now() - timedelta(hours=max(1.0, self.cfg.reveal_lookback_hours)))
        found = 0
        for media_type, client, id_field in (("episode", self.sonarr, "episodeId"), ("movie", self.radarr, "movieId")):
            try:
                history = client.imports_since(start)
            except ApiError as e:
                log.warning("Udgivelse: kunne ikke hente nye importer fra %s (%s)", client.name, e)
                continue
            for h in history:
                media_id, when = h.get(id_field), parse_date(h.get("date"))
                if not media_id or when is None or when < start:
                    continue
                found += self._add(media_type, int(media_id), when, h)
        return found

    def _add(self, media_type: str, media_id: int, when: datetime, h: dict) -> int:
        key, at = f"{media_type}:{media_id}", iso(when)
        if self.db.conn.execute("SELECT 1 FROM reveal WHERE key = ? AND import_at = ?", (key, at)).fetchone():
            return 0
        path = (h.get("data") or {}).get("importedPath") or self._file_path(media_type, media_id)
        if not path:
            log.warning("Udgivelse: kender ikke filen til %s - den vises efter sikkerhedsnettet", key)
        title = h.get("sourceTitle") or key
        original = self._original_danish(media_type, media_id, h.get("seriesId"))
        # En ny import af samme film/afsnit afløser en ældre, der stadig venter (filen er skiftet ud).
        self.db.conn.execute(
            "UPDATE reveal SET status = 'erstattet', reason = 'ny fil importeret' "
            "WHERE key = ? AND status = 'venter' AND import_at < ?",
            (key, at),
        )
        self.db.conn.execute(
            "INSERT INTO reveal (key, import_at, media_type, media_id, title, path, folder, status, reason, danish) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'venter', ?, ?)",
            (
                key,
                at,
                media_type,
                media_id,
                title,
                path or "",
                os.path.dirname(path) if path else "",
                "venter på dansk tekst",
                "original" if original else None,
            ),
        )
        self.db.conn.commit()
        log.info("Udgivelse: %s er skjult, indtil den har dansk tale eller tekst", title)
        return 1

    def _file_path(self, media_type: str, media_id: int) -> str | None:
        try:
            if media_type == "movie":
                return ((self.radarr.movie(media_id) or {}).get("movieFile") or {}).get("path")
            ep = self.sonarr.episode(media_id) or {}
            if ep.get("episodeFileId"):
                return (self.sonarr.episode_file(int(ep["episodeFileId"])) or {}).get("path")
        except ApiError as e:
            log.warning("Udgivelse: kunne ikke slå filen til %s:%d op (%s)", media_type, media_id, e)
        return None

    def _original_danish(self, media_type: str, media_id: int, series_id) -> bool:
        try:
            if media_type == "movie":
                return _danish_name((self.radarr.movie(media_id) or {}).get("originalLanguage"))
            if series_id:
                return _danish_name((self.sonarr.series(int(series_id)) or {}).get("originalLanguage"))
        except ApiError:
            pass
        return False

    # --- 3: er filen klar? ---------------------------------------------------------------------------
    def ready(self, row) -> str | None:
        """Grunden til, at filen må vises (en nøgle i REASONS), eller None hvis den stadig mangler dansk."""
        if row["danish"] == "original":
            return "original"
        path = row["path"]
        if not path:
            return None
        if danish_on_disk(path):
            return "fil"
        danish = row["danish"]
        if danish is None:  # lyd- og tekstspor ændrer sig ikke - filen læses kun én gang
            audio = audio_languages(path, self.run_cmd)
            subs = subtitle_languages(path, self.run_cmd) if audio is not None else None
            if audio is None or subs is None:
                return None  # kunne ikke læses lige nu - prøv igen næste gang
            danish = "lyd" if DANISH_AUDIO & set(audio) else "tekst" if DANISH_AUDIO & set(subs) else ""
            self.db.conn.execute(
                "UPDATE reveal SET danish = ? WHERE key = ? AND import_at = ?", (danish, row["key"], row["import_at"])
            )
            self.db.conn.commit()
        return danish or None

    # --- 4: vis mapperne ----------------------------------------------------------------------------------
    def reveal(self) -> int:
        now = self.now()
        safety = timedelta(hours=max(0.0, self.cfg.reveal_safety_hours))
        folders: dict[str, list[tuple]] = defaultdict(list)  # mappe -> [(række, grund, sikkerhedsnet)]
        for row in self.db.conn.execute("SELECT * FROM reveal WHERE status = 'venter' ORDER BY import_at").fetchall():
            path = row["path"]
            if not path:  # filen var ukendt ved importen - prøv at slå den op igen
                path = self._file_path(row["media_type"], int(row["media_id"])) or ""
                if path:
                    self._set(row, path=path, folder=os.path.dirname(path))
                    row = self.db.conn.execute(
                        "SELECT * FROM reveal WHERE key = ? AND import_at = ?", (row["key"], row["import_at"])
                    ).fetchone()
            if path and not os.path.exists(path):
                if os.path.isdir(row["folder"]):  # mappen findes, filen ikke: opgraderet eller slettet
                    self._set(row, status="erstattet", reason="filen findes ikke længere")
                continue  # mappen mangler: drevet er måske nede - vent
            why = self.ready(row)
            age = now - (parse_date(row["import_at"]) or now)
            if why is None and age < safety:
                self._set(row, reason="venter på dansk tekst")
            folders[row["folder"] or row["key"]].append((row, why, why is None and age >= safety))
        shown = 0
        for folder, rows in folders.items():
            waiting = any(why is None and not late for _, why, late in rows)
            if waiting and not any(late for _, _, late in rows):
                continue  # mindst én fil i mappen mangler stadig dansk
            # Har en fil nået sikkerhedsnettet, vises mappen - også selv om et nyere afsnit i den ikke er klar
            # (ellers kunne en daglig serie holde mappen skjult i det uendelige). Det nyere tæller som sikkerhedsnet.
            shown += self._show(folder, [(r, why, late or why is None) for r, why, late in rows])
        return shown

    def _show(self, folder: str, rows: list[tuple]) -> int:
        names = ", ".join(r["title"] for r, _, _ in rows)
        if self.cfg.dry_run:
            log.info("DRY_RUN: ville bede Plex vise %s", names)
            return 0
        path = next((r["path"] for r, _, _ in rows if r["path"]), "")
        if path:
            done = refresh_folder(self.cfg, self.http, path)
            if done is None:
                if not self._warned_plex:
                    log.warning("Udgivelse: PLEX_URL eller Plex-tokenet mangler - kan ikke vise nye filer i Plex")
                    self._warned_plex = True
                return 0
            if not done:
                return 0  # Plex svarede ikke - prøv igen næste gang (refresh_folder har skrevet advarslen)
        stamp = iso(self.now())
        for r, why, late in rows:
            by = "sikkerhedsnet" if late else "klar"
            reason = REASONS[why] if why else f"intet dansk efter {self.cfg.reveal_safety_hours:g} timer"
            if late and not why and not self._overdue(r):
                reason = "vist sammen med en ældre fil i samme mappe (sikkerhedsnet)"
            self._set(r, status="vist", reason=reason, shown_at=stamp, shown_by=by)
            if late:
                log.warning(
                    "Udgivelse: %s vises UDEN dansk tekst (sikkerhedsnet efter %g timer)",
                    r["title"],
                    self.cfg.reveal_safety_hours,
                )
            else:
                log.info("Udgivelse: %s vises nu i Plex (%s)", r["title"], reason)
        return len(rows)

    def _overdue(self, row) -> bool:
        at = parse_date(row["import_at"])
        return at is not None and self.now() - at >= timedelta(hours=max(0.0, self.cfg.reveal_safety_hours))

    def _set(self, row, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.conn.execute(
            f"UPDATE reveal SET {cols} WHERE key = ? AND import_at = ?",
            (*fields.values(), row["key"], row["import_at"]),
        )
        self.db.conn.commit()

    def cleanup(self) -> None:
        cutoff = iso(self.now() - timedelta(days=KEEP_DAYS))
        self.db.conn.execute("DELETE FROM reveal WHERE status != 'venter' AND import_at < ?", (cutoff,))
        self.db.conn.commit()

    def run_once(self) -> int:
        self.collect()
        shown = self.reveal()
        self.cleanup()
        self.last_run = self.now()
        return shown

    def loop(self) -> None:
        while True:
            try:
                self.run_once()
                self.last_error = None
            except Exception as e:  # tråden må aldrig dø
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Udgivelse: uventet fejl (%s) - prøver igen om lidt", self.last_error)
            self.wake.wait(max(10.0, self.cfg.reveal_interval_seconds))
            self.wake.clear()
            time.sleep(2)  # lille pause, så Bazarr har skrevet filen færdig


def start(cfg: Config, runner=None) -> Revealer | None:
    """Start udgivelsen i sin egen tråd, hvis REVEAL er slået til. Fejler aldrig."""
    if not cfg.reveal:
        log.info("Udgivelse: REVEAL=false - nye filer vises i Plex som før (via Autoscan)")
        return None
    try:
        db = Database(os.path.join(cfg.data_dir, "oversaetter.db"))  # egen forbindelse til tråden
        revealer = Revealer(cfg, db)
        if not cfg.translate_all_since:
            log.warning(
                "Udgivelse: TRANSLATE_ALL_SINCE er tom - nye titler uden Seerr-ønske oversættes ikke og vises først "
                "efter sikkerhedsnettet (%g timer)",
                cfg.reveal_safety_hours,
            )
        log.info(
            "Udgivelse: REVEAL=true - nye filer vises først i Plex med dansk tale/tekst, senest efter %g timer "
            "(tjek hvert %g sekund)",
            cfg.reveal_safety_hours,
            cfg.reveal_interval_seconds,
        )
        threading.Thread(target=revealer.loop, name="udgiv", daemon=True).start()
        if runner is not None:
            runner.revealer = revealer
        return revealer
    except Exception as e:
        log.warning("Udgivelse: kunne ikke starte (%s: %s)", type(e).__name__, e)
        return None
