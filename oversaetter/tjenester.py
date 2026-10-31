"""Trin B9 (31.10.2026): dansk lyd og danske streamingtjenester.

- Dansk lyd: originalsproget fra Radarr/Sonarr (gratis og lokalt), ellers fra TMDB, og et dansk lydspor i filen
  (ffprobe). Titler med dansk lyd oversættes aldrig.
- Danske streamingtjenester: TMDB "watch/providers", landet DK, typerne flatrate, free og ads (ikke rent/buy).
  TMDB ser kun, hvad der ligger på tjenesterne nu - ikke hvad der kommer.

Alle opslag gemmes i databasens tabel lookups, så de højst laves én gang i døgnet (originalsprog og lydspor kun én
gang). Fejl stopper aldrig noget: så regnes titlen for at være uden dansk lyd og uden dansk tjeneste.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta

from .clients import ApiError
from .db import Database
from .kontekst import Tmdb

log = logging.getLogger("oversaetter")

DANISH_NAMES = {"danish", "dansk"}
DANISH_AUDIO = {"da", "dan", "danish", "dansk"}
PROVIDER_TYPES = ("flatrate", "free", "ads")
RECHECK = timedelta(hours=24)  # tjenesterne slås op igen højst så ofte
PROBE_TIMEOUT = 120.0  # ffprobe læser kun filens hoved, men filerne ligger på et netværksdrev


def arr_language(data: dict) -> str | None:
    """Originalsproget fra Radarr/Sonarr: "da" for dansk, ellers sprogets navn. None, hvis feltet mangler."""
    lang = data.get("originalLanguage")
    name = str(lang.get("name") or "").strip().lower() if isinstance(lang, dict) else ""
    if not name or name in ("unknown", "any", "original"):
        return None
    return "da" if name in DANISH_NAMES else name


def dk_providers(data: dict) -> list[str]:
    """Navnene på de danske tjenester (flatrate, free, ads) i TMDB's svar fra watch/providers."""
    dk = (data.get("results") or {}).get("DK") or {}
    names: list[str] = []
    for kind in PROVIDER_TYPES:
        for p in dk.get(kind) or []:
            name = str(p.get("provider_name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def audio_languages(video: str, run_cmd=subprocess.run, timeout: float = PROBE_TIMEOUT) -> list[str] | None:
    """Sprogene på filens lydspor (ffprobe). None, hvis filen ikke kunne læses."""
    args = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index:stream_tags=language"]
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


class Lookups:
    """Opslag i TMDB og ffprobe med svarene gemt i databasen."""

    def __init__(self, db: Database, tmdb: Tmdb | None, run_cmd=None, dry_run: bool = False):
        self.db, self.tmdb, self.run_cmd, self.dry_run = db, tmdb, run_cmd, dry_run

    def _cached(self, key: str, max_age: timedelta | None = None):
        """(fundet, værdi). max_age None = gemt for altid."""
        hit = self.db.lookup(key)
        if hit is None:
            return False, None
        value, when = hit
        if max_age is not None and (when is None or datetime.now(UTC) - when >= max_age):
            return False, json.loads(value) if value else None
        return True, json.loads(value) if value else None

    def _save(self, key: str, value) -> None:
        if not self.dry_run:
            self.db.save_lookup(key, json.dumps(value, ensure_ascii=False))

    def tv_id(self, series: dict) -> int | None:
        """Seriens TMDB-id: fra Sonarr, ellers via tvdbId (TMDB find). None, hvis det ikke kan findes."""
        if series.get("tmdbId"):
            return int(series["tmdbId"])
        tvdb = series.get("tvdbId")
        if not tvdb or self.tmdb is None:
            return None
        key = f"tvdb:{int(tvdb)}"
        found, value = self._cached(key, RECHECK)
        if found:
            return value
        try:
            data = self.tmdb._get(f"/find/{int(tvdb)}", external_source="tvdb_id")
            results = data.get("tv_results") or []
            value = int(results[0]["id"]) if results and results[0].get("id") else None
        except (ApiError, ValueError, TypeError, KeyError):
            value = None
        self._save(key, value)
        return value

    def tmdb_language(self, kind: str, tmdb_id: int | None) -> str | None:
        """Originalsproget fra TMDB ("da", "en" ...), eller None. kind er "movie" eller "tv"."""
        if self.tmdb is None or not tmdb_id:
            return None
        key = f"lang:{kind}:{int(tmdb_id)}"
        found, value = self._cached(key)
        if found and value:
            return value
        try:
            data = self.tmdb._get(f"/{kind}/{int(tmdb_id)}")
            value = str(data.get("original_language") or "").strip().lower() or None
        except ApiError as e:
            log.debug("TMDB kunne ikke give originalsproget for %s:%s: %s", kind, tmdb_id, e)
            return None
        if value:
            self._save(key, value)
        return value

    def danish_audio_track(self, item_key: str, video: str) -> bool:
        """Har filen et dansk lydspor? Svaret gemmes pr. emne og fil; en ulæselig fil prøves igen om et døgn."""
        if self.run_cmd is None or not video:
            return False
        key = f"audio:{item_key}"
        found, value = self._cached(key)
        if found and isinstance(value, dict) and value.get("path") == video:
            if not value.get("error"):
                return bool(value.get("danish"))
            hit = self.db.lookup(key)
            if hit and hit[1] and datetime.now(UTC) - hit[1] < RECHECK:
                return False  # filen kunne ikke læses for nylig - prøves igen om et døgn
        langs = audio_languages(video, self.run_cmd)
        if langs is None:
            log.debug("ffprobe kunne ikke læse lydsporene i %s", video)
            self._save(key, {"path": video, "error": True})
            return False
        danish = any(lang in DANISH_AUDIO for lang in langs)
        self._save(key, {"path": video, "danish": danish})
        return danish

    def providers(self, kind: str, tmdb_id: int | None, label: str) -> list[str] | None:
        """Danske tjenester for titlen (højst ét opslag i døgnet pr. film/serie). None, hvis opslaget fejlede."""
        if self.tmdb is None:
            return None
        key = f"providers:{kind}:{int(tmdb_id) if tmdb_id else 'ukendt:' + label}"
        found, value = self._cached(key, RECHECK)
        if found:
            return value.get("names") if isinstance(value, dict) else None
        failed_before = isinstance(value, dict) and "error" in value
        if not tmdb_id:
            why = "intet TMDB-id"
        else:
            try:
                names = dk_providers(self.tmdb._get(f"/{kind}/{int(tmdb_id)}/watch/providers"))
                self._save(key, {"names": names})
                return names
            except ApiError as e:
                why = str(e)
        if not failed_before:
            log.warning(
                "Kunne ikke slå danske streamingtjenester op for %s hos TMDB (%s) - oversætter uden at vente",
                label,
                why,
            )
        self._save(key, {"error": why})
        return None
