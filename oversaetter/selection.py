"""Valg af kandidater: hvad mangler dansk, er ønsket/mærket, og har en engelsk kilde."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .clients import Bazarr, Radarr, SeerrIndex, Sonarr, parse_date
from .config import Config
from .db import Database

log = logging.getLogger("oversaetter")

SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx")
DANISH_TAGS = {"da", "dan", "danish", "dansk"}
SKIP_RECHECK = timedelta(hours=24)


@dataclass
class Candidate:
    media_type: str  # "movie" eller "episode"
    media_id: int  # radarrId eller sonarrEpisodeId
    title: str
    reason: str  # "Seerr" eller "mærke"
    video_path: str = ""
    source_path: str | None = None
    subtitles_id: int | None = None
    source_desc: str = ""

    @property
    def key(self) -> str:
        return f"{self.media_type}:{self.media_id}"

    @property
    def da_path(self) -> str:
        return os.path.splitext(self.video_path)[0] + ".da.srt"


@dataclass
class Stats:
    wanted: int = 0
    not_selected: int = 0
    waiting: int = 0
    done_before: int = 0
    skipped: list[str] = field(default_factory=list)


def wants_danish(entry: dict) -> bool:
    return any(m.get("code2") == "da" and not m.get("forced") for m in entry.get("missing_subtitles") or [])


def danish_on_disk(video_path: str) -> str | None:
    """Find en eksisterende dansk undertekstfil ved siden af videoen."""
    directory, name = os.path.split(video_path)
    stem = os.path.splitext(name)[0]
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for other in names:
        low = other.lower()
        if not other.startswith(stem + ".") or not low.endswith(SUB_EXTS):
            continue
        tokens = low[len(stem) + 1 :].split(".")[:-1]
        if DANISH_TAGS & set(tokens):
            return os.path.join(directory, other)
    return None


def has_danish(subtitles: list[dict]) -> dict | None:
    for sub in subtitles or []:
        if sub.get("code2") == "da" or (sub.get("code3") or "").lower() == "dan":
            return sub
    return None


def choose_source(subtitles: list[dict]) -> tuple[str | None, int | None, str]:
    """Foretræk ekstern engelsk .srt (ikke forced, helst ikke HI), ellers indlejret engelsk spor."""
    english = [s for s in subtitles or [] if s.get("code2") == "en" and not s.get("forced")]
    external = [
        s for s in english if s.get("path") and str(s["path"]).lower().endswith(".srt") and os.path.isfile(s["path"])
    ]
    external.sort(key=lambda s: bool(s.get("hi")))
    if external:
        s = external[0]
        return s["path"], None, f"ekstern engelsk{' (HI)' if s.get('hi') else ''}: {os.path.basename(s['path'])}"
    embedded = [s for s in english if not s.get("path") and s.get("embedded_track_id") is not None and s.get("id")]
    embedded.sort(key=lambda s: bool(s.get("hi")))
    if embedded:
        s = embedded[0]
        return None, int(s["id"]), f"indlejret engelsk spor (id {s['id']}{', HI' if s.get('hi') else ''})"
    return None, None, ""


def _age(date_added: str | None, video_path: str, now: datetime) -> timedelta | None:
    dt = parse_date(date_added)
    if dt is None and video_path and os.path.exists(video_path):
        dt = datetime.fromtimestamp(os.path.getmtime(video_path), tz=UTC)
    return None if dt is None else now - dt


class Selector:
    def __init__(self, cfg: Config, bazarr: Bazarr, radarr: Radarr, sonarr: Sonarr, seerr: SeerrIndex, db: Database):
        self.cfg, self.bazarr, self.radarr, self.sonarr, self.seerr, self.db = cfg, bazarr, radarr, sonarr, seerr, db
        self._series: dict[int, dict] = {}
        self._movies: dict[int, dict] | None = None
        self._tag_id: int | None | bool = False  # False = ikke slået op endnu

    def tag_id(self) -> int | None:
        if self._tag_id is False:
            wanted = self.cfg.translate_tag.strip().lower()
            self._tag_id = next(
                (int(t["id"]) for t in self.sonarr.tags() if str(t.get("label", "")).lower() == wanted), None
            )
        return self._tag_id

    def movie(self, radarr_id: int) -> dict:
        if self._movies is None:
            self._movies = {int(m["id"]): m for m in self.radarr.movies()}
        return self._movies.get(radarr_id) or self.radarr.movie(radarr_id)

    def series(self, series_id: int) -> dict:
        if series_id not in self._series:
            self._series[series_id] = self.sonarr.series(series_id)
        return self._series[series_id]

    def _db_blocks(self, key: str, now: datetime, stats: Stats) -> bool:
        row = self.db.get(key)
        if row is None:
            return False
        if row["status"] in ("faerdig", "fejlet", "i_gang", "ikke_muligt"):
            stats.done_before += 1
            return True
        if row["status"] == "sprunget_over":
            updated = parse_date(row["updated_at"])
            if updated and now - updated < SKIP_RECHECK:
                stats.done_before += 1
                return True
        return False

    def eligibility_movie(self, entry: dict, now: datetime) -> tuple[str | None, bool]:
        """(grund, venter). grund er None hvis filmen ikke skal oversættes nu."""
        movie = self.movie(int(entry["radarrId"]))
        if not self.seerr.movie_wanted(movie.get("tmdbId")):
            return None, False
        movie_file = movie.get("movieFile") or {}
        age = _age(movie_file.get("dateAdded"), movie_file.get("path", ""), now)
        if age is None or age < timedelta(minutes=self.cfg.seerr_delay_minutes):
            return None, True
        return "Seerr", False

    def eligibility_episode(self, entry: dict, now: datetime) -> tuple[str | None, bool]:
        # Først serien (gemmes mellem afsnit), så afsnittet kun slås op når serien er relevant.
        series = self.series(int(entry["sonarrSeriesId"]))
        tag = self.tag_id()
        via_tag = tag is not None and tag in (series.get("tags") or [])
        if not via_tag and not self.seerr.series_maybe_wanted(series.get("tvdbId"), series.get("tmdbId")):
            return None, False
        ep = self.sonarr.episode(int(entry["sonarrEpisodeId"]))
        via_seerr = self.seerr.episode_wanted(series.get("tvdbId"), series.get("tmdbId"), ep.get("seasonNumber"))
        if not via_seerr and not via_tag:
            return None, False
        file_info = ep.get("episodeFile") or {}
        if not file_info and ep.get("episodeFileId"):
            file_info = self.sonarr.episode_file(int(ep["episodeFileId"]))
        age = _age(file_info.get("dateAdded"), file_info.get("path", ""), now)
        if age is None:
            return None, True
        if via_seerr and age >= timedelta(minutes=self.cfg.seerr_delay_minutes):
            return "Seerr", False
        if via_tag and age >= timedelta(hours=self.cfg.tag_delay_hours):
            return "mærke", False
        return None, True

    def skip(self, cand: Candidate, why: str, stats: Stats) -> None:
        stats.skipped.append(f"{cand.title}: {why}")
        log.info("Springer over: %s - %s", cand.title, why)
        if not self.cfg.dry_run:
            self.db.upsert(
                cand.key,
                media_type=cand.media_type,
                media_id=cand.media_id,
                title=cand.title,
                status="sprunget_over",
                reason=why,
                video_path=cand.video_path,
            )

    def find(self, now: datetime | None = None) -> tuple[list[Candidate], Stats]:
        now = now or datetime.now(UTC)
        stats = Stats()
        candidates: list[Candidate] = []
        entries = [("movie", e) for e in self.bazarr.wanted_movies()] + [
            ("episode", e) for e in self.bazarr.wanted_episodes()
        ]
        for media_type, entry in entries:
            if not wants_danish(entry):
                continue
            stats.wanted += 1
            if media_type == "movie":
                media_id = int(entry["radarrId"])
                title = entry.get("title") or f"film {media_id}"
            else:
                media_id = int(entry["sonarrEpisodeId"])
                title = f"{entry.get('seriesTitle', 'serie')} {entry.get('episode_number', '')}".strip()
            cand = Candidate(media_type, media_id, title, reason="")
            if self._db_blocks(cand.key, now, stats):
                continue
            try:
                if media_type == "movie":
                    reason, waiting = self.eligibility_movie(entry, now)
                else:
                    reason, waiting = self.eligibility_episode(entry, now)
            except Exception as e:
                log.warning("Kunne ikke slå %s op i Radarr/Sonarr/Seerr: %s", title, e)
                continue
            if reason is None:
                if waiting:
                    stats.waiting += 1
                else:
                    stats.not_selected += 1
                continue
            cand.reason = reason
            item = self.bazarr.item(media_type, media_id)
            if not item:
                log.warning("Bazarr kender ikke %s", title)
                continue
            cand.video_path = item.get("path") or ""
            subtitles = item.get("subtitles") or []
            da = has_danish(subtitles)
            if da:
                kind = "ekstern" if da.get("path") else "indlejret"
                self.skip(cand, f"har allerede dansk ({kind}{', forced' if da.get('forced') else ''})", stats)
                continue
            on_disk = danish_on_disk(cand.video_path) if cand.video_path else None
            if on_disk:
                self.skip(cand, f"har allerede dansk fil: {os.path.basename(on_disk)}", stats)
                continue
            path, sub_id, desc = choose_source(subtitles)
            if not path and sub_id is None:
                self.skip(cand, "ingen engelsk tekstkilde", stats)
                continue
            cand.source_path, cand.subtitles_id, cand.source_desc = path, sub_id, desc
            candidates.append(cand)
        return candidates, stats
