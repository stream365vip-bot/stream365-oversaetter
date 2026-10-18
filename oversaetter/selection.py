"""Valg af emner: hvad mangler dansk, er ønsket/mærket, har haft en gyldig søgning og har en engelsk kilde."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .clients import ApiError, Bazarr, Radarr, SeerrIndex, Sonarr, parse_date
from .config import Config
from .db import Database
from .hi import hi_filename

log = logging.getLogger("oversaetter")

SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx")
IMAGE_EXTS = (".sup", ".sub", ".idx")
IMAGE_FORMATS = {"pgs", "hdmv_pgs_subtitle", "vobsub", "dvd_subtitle", "dvdsub"}
MAX_EXAMPLES = 5
DANISH_TAGS = {"da", "dan", "danish", "dansk"}
SKIP_RECHECK = timedelta(hours=24)
EN_SEARCH_AGAIN = timedelta(hours=24)  # mindste tid mellem to søgninger efter engelsk til samme emne


@dataclass
class Candidate:
    media_type: str  # "movie" eller "episode"
    media_id: int  # radarrId eller sonarrEpisodeId
    title: str
    reason: str  # "Seerr" eller "mærke"
    video_path: str = ""
    series_id: int | None = None  # sonarrSeriesId (kun afsnit)
    hi: bool = False  # Bazarr mangler den danske tekst som HI
    source_path: str | None = None
    subtitles_id: int | None = None
    source_desc: str = ""
    source_hi: bool = False  # Bazarr eller filnavnet siger, at den eksterne engelske fil er HI/SDH

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
    searched: int = 0
    found_danish: int = 0
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


def external_hi(sub: dict) -> bool:
    """Bazarr markerer filen som HI, eller filnavnet gør (.en.hi.srt / .en.sdh.srt)."""
    return bool(sub.get("hi")) or hi_filename(str(sub.get("path") or ""))


def choose_source(subtitles: list[dict], prefer_hi: bool = True) -> tuple[str | None, int | None, str]:
    """Foretræk ekstern engelsk .srt (ikke forced, helst HI), ellers indlejret engelsk spor.

    prefer_hi=False: helst ikke HI (efterbehandlingens engelske reference, som før B4-opfølgningen).
    """
    english = [s for s in subtitles or [] if s.get("code2") == "en" and not s.get("forced")]
    external = [
        s for s in english if s.get("path") and str(s["path"]).lower().endswith(".srt") and os.path.isfile(s["path"])
    ]
    external.sort(key=lambda s: external_hi(s) != prefer_hi)  # stabil: første foretrukne vinder
    if external:
        s = external[0]
        tag = " (HI)" if external_hi(s) else ""
        return s["path"], None, f"ekstern engelsk{tag}: {os.path.basename(s['path'])}"
    embedded = [s for s in english if not s.get("path") and s.get("embedded_track_id") is not None and s.get("id")]
    embedded.sort(key=lambda s: bool(s.get("hi")) != prefer_hi)
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


def missing_hi(entry: dict) -> bool:
    return any(
        m.get("code2") == "da" and not m.get("forced") and m.get("hi") for m in entry.get("missing_subtitles") or []
    )


class Selector:
    def __init__(self, cfg: Config, bazarr: Bazarr, radarr: Radarr, sonarr: Sonarr, seerr: SeerrIndex, db: Database):
        self.cfg, self.bazarr, self.radarr, self.sonarr, self.seerr, self.db = cfg, bazarr, radarr, sonarr, seerr, db
        self._series: dict[int, dict] = {}
        self._movies: dict[int, dict] | None = None
        self._tag_ids: dict[str, int | None] = {}  # "radarr"/"sonarr" -> id på mærket (None = findes ikke)
        self.need_english: list[Candidate] = []  # ingen dansk og ingen engelsk kilde: Bazarr skal søge efter engelsk

    def _tag(self, name: str, api) -> int | None:
        if name not in self._tag_ids:
            wanted = self.cfg.translate_tag.strip().lower()
            self._tag_ids[name] = next(
                (int(t["id"]) for t in api.tags() if str(t.get("label", "")).lower() == wanted), None
            )
        return self._tag_ids[name]

    def tag_id(self) -> int | None:
        """Mærket i Sonarr."""
        return self._tag("sonarr", self.sonarr)

    def radarr_tag_id(self) -> int | None:
        return self._tag("radarr", self.radarr)

    def all_movies(self) -> dict[int, dict]:
        if self._movies is None:
            self._movies = {int(m["id"]): m for m in self.radarr.movies()}
        return self._movies

    def movie(self, radarr_id: int) -> dict:
        return self.all_movies().get(radarr_id) or self.radarr.movie(radarr_id)

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

    def eligibility_movie(self, entry: dict) -> tuple[str | None, str]:
        """(grund, forklaring). grund er None hvis filmen hverken er Seerr-ønske eller har mærket i Radarr."""
        movie = self.movie(int(entry["radarrId"]))
        if self.seerr.movie_wanted(movie.get("tmdbId")):
            return "Seerr", ""
        tag = self.radarr_tag_id()
        if tag is not None and tag in (movie.get("tags") or []):
            return "mærke", ""
        return None, f"ikke Seerr-ønske og mangler mærket {self.cfg.translate_tag}"

    def eligibility_episode(self, entry: dict) -> tuple[str | None, str]:
        # Først serien (gemmes mellem afsnit), så afsnittet kun slås op når serien er relevant.
        series = self.series(int(entry["sonarrSeriesId"]))
        tag = self.tag_id()
        via_tag = tag is not None and tag in (series.get("tags") or [])
        no_tag = f"mangler mærket {self.cfg.translate_tag}"
        if not via_tag and not self.seerr.series_maybe_wanted(series.get("tvdbId"), series.get("tmdbId")):
            return None, f"ikke Seerr-ønske og {no_tag}"
        if via_tag:
            return "mærke", ""
        ep = self.sonarr.episode(int(entry["sonarrEpisodeId"]))
        if self.seerr.episode_wanted(series.get("tvdbId"), series.get("tmdbId"), ep.get("seasonNumber")):
            return "Seerr", ""
        return None, f"sæson {ep.get('seasonNumber')} er ikke Seerr-ønske og {no_tag}"

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

    def found_danish(self, cand: Candidate, stats: Stats) -> None:
        why = "gyldig søgning fandt dansk - intet at gøre"
        stats.found_danish += 1
        stats.example("fandt dansk", cand.title, why)
        log.info("%s: %s", cand.title, why)
        if not self.cfg.dry_run:
            self.db.upsert(cand.key, status="faerdig", reason="gyldig søgning fandt dansk")

    def no_english(self, cand: Candidate, why: str, stats: Stats) -> None:
        """Ingen dansk og ingen brugbar engelsk kilde: søg efter engelsk, eller spring over til næste runde.

        En ny søgning efter engelsk startes tidligst EN_SEARCH_AGAIN efter den forrige gyldige. Indtil da tjekkes
        der billigt hver runde (uden søgning), om der er dukket en engelsk tekst op.
        """
        row = self.db.get(cand.key)
        en_at = parse_date(row["en_search_at"]) if row is not None else None
        now = datetime.now(UTC)
        searching = row is not None and bool(row["search_started_at"])
        if searching or en_at is None or now - en_at >= EN_SEARCH_AGAIN:
            self.need_english.append(cand)
            stats.example("mangler engelsk", cand.title, f"{why} - søger efter engelsk")
            if not self.cfg.dry_run:
                self.db.upsert(cand.key, status="mangler_engelsk", reason=why, video_path=cand.video_path)
            return
        next_at = (en_at + EN_SEARCH_AGAIN).astimezone()
        full = (
            f"{why} (søgning efter engelsk fandt intet - tjekker hver runde, "
            f"ny søgning tidligst {next_at:%d.%m. kl. %H:%M})"
        )
        stats.skipped.append(f"{cand.title}: {full}")
        stats.example("sprunget over: ingen engelsk", cand.title, full)
        if row["status"] == "mangler_engelsk" and row["reason"] == full:
            log.debug("Springer over: %s - %s", cand.title, full)
            return
        log.info("Springer over: %s - %s", cand.title, full)
        if not self.cfg.dry_run:
            self.db.upsert(cand.key, status="mangler_engelsk", reason=full)

    def after_search(self, cand: Candidate, item: dict | None, stats: Stats) -> Candidate | None:
        """Efter en gyldig søgning: mangler emnet stadig dansk, og er der en engelsk kilde?"""
        if not item:
            log.warning("Bazarr kender ikke %s", cand.title)
            stats.example("fejl", cand.title, "Bazarr kender den ikke")
            return None
        cand.video_path = item.get("path") or cand.video_path
        subtitles = item.get("subtitles") or []
        on_disk = danish_on_disk(cand.video_path) if cand.video_path else None
        if not wants_danish(item) or has_danish(subtitles) or on_disk:
            self.found_danish(cand, stats)
            return None
        path, sub_id, desc = choose_source(subtitles)
        if not path and sub_id is None:
            self.no_english(cand, no_source_reason(subtitles), stats)
            return None
        cand.source_path, cand.subtitles_id, cand.source_desc = path, sub_id, desc
        cand.source_hi = any(external_hi(s) for s in subtitles if path and s.get("path") == path)
        row = self.db.get(cand.key)
        if row is not None and row["status"] == "mangler_engelsk":
            log.info("Engelsk fundet til %s - oversætter", cand.title)
        stats.example("oversætter", cand.title, f"gyldig søgning fandt ikke dansk - oversætter ({cand.reason}, {desc})")
        return cand

    def find(self, now: datetime | None = None) -> tuple[list[Candidate], list[Candidate], Stats]:
        """(klar til oversættelse, mangler gyldig søgning, tal til loggen)."""
        now = now or datetime.now(UTC)
        stats = Stats()
        candidates: list[Candidate] = []
        to_search: list[Candidate] = []
        movies, episodes = self.bazarr.wanted_movies(), self.bazarr.wanted_episodes()
        stats.bazarr_movies, stats.bazarr_episodes = len(movies), len(episodes)
        entries = [("movie", e) for e in movies] + [("episode", e) for e in episodes]
        for media_type, entry in entries:
            if not wants_danish(entry):
                continue
            stats.wanted += 1
            if media_type == "movie":
                stats.wanted_movies += 1
                media_id = int(entry["radarrId"])
                title = entry.get("title") or f"film {media_id}"
                series_id = None
            else:
                stats.wanted_episodes += 1
                media_id = int(entry["sonarrEpisodeId"])
                title = f"{entry.get('seriesTitle', 'serie')} {entry.get('episode_number', '')}".strip()
                series_id = int(entry["sonarrSeriesId"])
            cand = Candidate(media_type, media_id, title, reason="", series_id=series_id, hi=missing_hi(entry))
            if self._db_blocks(cand.key, now, stats):
                row = self.db.get(cand.key)
                stats.example("allerede behandlet", title, f"allerede behandlet (status {row['status']})")
                continue
            try:
                if media_type == "movie":
                    reason, why = self.eligibility_movie(entry)
                else:
                    reason, why = self.eligibility_episode(entry)
            except Exception as e:
                msg = str(e) if isinstance(e, ApiError) else f"{type(e).__name__}: {e}"
                if msg not in stats.errors:
                    log.warning("Kunne ikke slå %s op i Radarr/Sonarr: %s", title, msg)
                stats.errors[msg] = stats.errors.get(msg, 0) + 1
                stats.example("fejl", title, f"fejl ved opslag: {msg}")
                continue
            if reason is None:
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
            da = has_danish(item.get("subtitles") or [])
            if da:
                kind = "ekstern" if da.get("path") else "indlejret"
                self.skip(cand, f"har allerede dansk ({kind}{', forced' if da.get('forced') else ''})", stats)
                continue
            on_disk = danish_on_disk(cand.video_path) if cand.video_path else None
            if on_disk:
                self.skip(cand, f"har allerede dansk fil: {os.path.basename(on_disk)}", stats)
                continue
            row = self.db.get(cand.key)
            if row is None or not row["search_at"]:
                to_search.append(cand)  # regel: aldrig oversættelse uden en gyldig søgning
                continue
            ready = self.after_search(cand, item, stats)
            if ready:
                candidates.append(ready)
        return candidates, to_search, stats
