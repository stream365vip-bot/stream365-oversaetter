"""Valg af kandidater: hvad mangler dansk, er ønsket/mærket, og har en engelsk kilde."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .clients import ApiError, Bazarr, Radarr, SeerrIndex, Sonarr, parse_date
from .config import Config
from .db import Database

log = logging.getLogger("oversaetter")

SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx")
IMAGE_EXTS = (".sup", ".sub", ".idx")
IMAGE_FORMATS = {"pgs", "hdmv_pgs_subtitle", "vobsub", "dvd_subtitle", "dvdsub"}
MAX_EXAMPLES = 5
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
    # Bazarrs rå tal (hele wanted-listen) og hvor mange af dem der mangler dansk.
    bazarr_movies: int = 0
    bazarr_episodes: int = 0
    wanted_movies: int = 0
    wanted_episodes: int = 0
    examples: list[tuple[str, str]] = field(default_factory=list)  # (slags, "titel - grund")
    errors: dict[str, int] = field(default_factory=dict)  # fejltekst -> antal emner

    def example(self, kind: str, title: str, why: str) -> None:
        self.examples.append((kind, f"{title} - {why}"))

    def pick_examples(self, n: int = MAX_EXAMPLES) -> list[str]:
        """Op til n eksempler: først ét af hver slags, så resten i rækkefølge."""
        picked: list[int] = []
        seen: set[str] = set()
        for i, (kind, _) in enumerate(self.examples):
            if kind not in seen:
                seen.add(kind)
                picked.append(i)
        picked += [i for i in range(len(self.examples)) if i not in picked]
        return [self.examples[i][1] for i in sorted(picked[:n])]


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


def no_source_reason(subtitles: list[dict]) -> str:
    """Hvorfor choose_source ikke fandt noget - kun til loggen."""
    english = [s for s in subtitles or [] if s.get("code2") == "en" and not s.get("forced")]
    if not english:
        return "ingen engelsk tekstkilde"
    if all(
        str(s.get("path") or "").lower().endswith(IMAGE_EXTS) or str(s.get("format") or "").lower() in IMAGE_FORMATS
        for s in english
    ):
        return "kun PGS/VobSub (billedbaseret engelsk tekst) - kan ikke oversættes"
    return "ingen engelsk tekstkilde, der kan bruges (engelsk findes, men ikke som .srt eller indlejret spor)"


def format_wait(left: timedelta) -> str:
    minutes = max(1, math.ceil(left.total_seconds() / 60))
    if minutes < 120:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} t {minutes} min" if minutes else f"{hours} t"


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

    def eligibility_movie(self, entry: dict, now: datetime) -> tuple[str | None, bool, str]:
        """(grund, venter, forklaring). grund er None hvis filmen ikke skal oversættes nu."""
        movie = self.movie(int(entry["radarrId"]))
        if not self.seerr.movie_wanted(movie.get("tmdbId")):
            return None, False, "ikke Seerr-ønske"
        movie_file = movie.get("movieFile") or {}
        age = _age(movie_file.get("dateAdded"), movie_file.get("path", ""), now)
        delay = timedelta(minutes=self.cfg.seerr_delay_minutes)
        if age is None:
            return None, True, "venter på forsinkelse (filens tidspunkt er ukendt)"
        if age < delay:
            return None, True, f"venter på forsinkelse, {format_wait(delay - age)} tilbage"
        return "Seerr", False, ""

    def eligibility_episode(self, entry: dict, now: datetime) -> tuple[str | None, bool, str]:
        # Først serien (gemmes mellem afsnit), så afsnittet kun slås op når serien er relevant.
        series = self.series(int(entry["sonarrSeriesId"]))
        tag = self.tag_id()
        via_tag = tag is not None and tag in (series.get("tags") or [])
        no_tag = f"mangler mærket {self.cfg.translate_tag}"
        if not via_tag and not self.seerr.series_maybe_wanted(series.get("tvdbId"), series.get("tmdbId")):
            return None, False, f"ikke Seerr-ønske og {no_tag}"
        ep = self.sonarr.episode(int(entry["sonarrEpisodeId"]))
        via_seerr = self.seerr.episode_wanted(series.get("tvdbId"), series.get("tmdbId"), ep.get("seasonNumber"))
        if not via_seerr and not via_tag:
            return None, False, f"sæson {ep.get('seasonNumber')} er ikke Seerr-ønske og {no_tag}"
        file_info = ep.get("episodeFile") or {}
        if not file_info and ep.get("episodeFileId"):
            file_info = self.sonarr.episode_file(int(ep["episodeFileId"]))
        age = _age(file_info.get("dateAdded"), file_info.get("path", ""), now)
        if age is None:
            return None, True, "venter på forsinkelse (filens tidspunkt er ukendt)"
        seerr_delay = timedelta(minutes=self.cfg.seerr_delay_minutes)
        tag_delay = timedelta(hours=self.cfg.tag_delay_hours)
        if via_seerr and age >= seerr_delay:
            return "Seerr", False, ""
        if via_tag and age >= tag_delay:
            return "mærke", False, ""
        left = min(d - age for ok, d in ((via_seerr, seerr_delay), (via_tag, tag_delay)) if ok)
        return None, True, f"venter på forsinkelse, {format_wait(left)} tilbage"

    def skip(self, cand: Candidate, why: str, stats: Stats) -> None:
        stats.skipped.append(f"{cand.title}: {why}")
        stats.example("sprunget over: " + why.split(" (")[0].split(":")[0], cand.title, why)
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
        movies, episodes = self.bazarr.wanted_movies(), self.bazarr.wanted_episodes()
        stats.bazarr_movies, stats.bazarr_episodes = len(movies), len(episodes)
        entries = [("movie", e) for e in movies] + [("episode", e) for e in episodes]
        for media_type, entry in entries:
            if not wants_danish(entry):
                continue
            stats.wanted += 1
            if media_type == "movie":
                stats.wanted_movies += 1
            else:
                stats.wanted_episodes += 1
            if media_type == "movie":
                media_id = int(entry["radarrId"])
                title = entry.get("title") or f"film {media_id}"
            else:
                media_id = int(entry["sonarrEpisodeId"])
                title = f"{entry.get('seriesTitle', 'serie')} {entry.get('episode_number', '')}".strip()
            cand = Candidate(media_type, media_id, title, reason="")
            if self._db_blocks(cand.key, now, stats):
                row = self.db.get(cand.key)
                stats.example("allerede behandlet", title, f"allerede behandlet (status {row['status']})")
                continue
            try:
                if media_type == "movie":
                    reason, waiting, why = self.eligibility_movie(entry, now)
                else:
                    reason, waiting, why = self.eligibility_episode(entry, now)
            except Exception as e:
                msg = str(e) if isinstance(e, ApiError) else f"{type(e).__name__}: {e}"
                if msg not in stats.errors:
                    log.warning("Kunne ikke slå %s op i Radarr/Sonarr: %s", title, msg)
                stats.errors[msg] = stats.errors.get(msg, 0) + 1
                stats.example("fejl", title, f"fejl ved opslag: {msg}")
                continue
            if reason is None:
                if waiting:
                    stats.waiting += 1
                    stats.example("venter", title, why)
                else:
                    stats.not_selected += 1
                    stats.example("ikke valgt: " + why, title, why)
                continue
            cand.reason = reason
            item = self.bazarr.item(media_type, media_id)
            if not item:
                log.warning("Bazarr kender ikke %s", title)
                stats.example("fejl", title, "Bazarr kender den ikke")
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
                self.skip(cand, no_source_reason(subtitles), stats)
                continue
            cand.source_path, cand.subtitles_id, cand.source_desc = path, sub_id, desc
            stats.example("valgt", title, f"valgt ({reason}) fra {desc}")
            candidates.append(cand)
        return candidates, stats
