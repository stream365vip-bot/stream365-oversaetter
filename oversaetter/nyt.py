"""Trin C1 (8.11.2026): alt nyt oversættes.

Film og afsnit, der er importeret i Radarr/Sonarr efter TRANSLATE_ALL_SINCE, er med i udvælgelsen uden Seerr-ønske
eller mærket oversaet. De findes i Radarr/Sonarr-historikken (downloadFolderImported), ikke ud fra filernes alder.

Bazarr kan kun søge og modtage en upload for en titel, der har en sprogprofil (uden profil fejler begge kald i
Bazarr). Nye titler uden profil får derfor profilen BAZARR_PROFILE, så de kommer på Bazarrs wanted-liste.

Sikring: en serie med profil får alle sine gamle afsnit på wanted-listen. Søger Bazarr selv efter manglende tekster
(wanted_search_frequency), ville den så søge til hele det gamle bibliotek og få tekstsiderne sat på pause. Derfor
sættes profilen kun, når Bazarrs egen søgning efter manglende tekster står på "Manually" for både serier og film.
Ellers skrives en advarsel, og ingen profiler sættes (oversættelsen af titler, der allerede har en profil, kører).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .clients import ApiError, Bazarr, Radarr, Sonarr, parse_date
from .config import Config

log = logging.getLogger("oversaetter")

MANUALLY = 876000  # Bazarrs værdi for "Manually" (100 år i timer)


@dataclass
class NewImports:
    """Nye importer: nøgle ("movie:12"/"episode:345") -> seneste importtidspunkt."""

    at: dict[str, datetime] = field(default_factory=dict)
    series_ids: set[int] = field(default_factory=set)
    movie_ids: set[int] = field(default_factory=set)
    titles: dict[str, str] = field(default_factory=dict)
    manual: set[str] = field(default_factory=set)  # trin C4: valgt med en knap i Fangarr

    def __contains__(self, key: str) -> bool:
        return key in self.at

    def get(self, key: str) -> datetime | None:
        return self.at.get(key)

    def __len__(self) -> int:
        return len(self.at)


def collect(cfg: Config, sonarr: Sonarr, radarr: Radarr, now: datetime | None = None) -> NewImports:
    """Find importer siden TRANSLATE_ALL_SINCE (højst NEW_LOOKBACK_DAYS tilbage). Tom, hvis trinnet er slået fra."""
    out = NewImports()
    since = cfg.all_since()
    if since is None:
        return out
    now = now or datetime.now(UTC)
    start = max(since, now - timedelta(days=max(1.0, cfg.new_lookback_days)))
    if start > now:
        return out

    def add(key: str, when: datetime | None, title: str) -> None:
        if when is None or when < since:
            return
        if key not in out.at or when > out.at[key]:
            out.at[key] = when
        out.titles.setdefault(key, title)

    for h in sonarr.imports_since(start):
        ep, series = h.get("episodeId"), h.get("seriesId")
        if not ep:
            continue
        add(f"episode:{int(ep)}", parse_date(h.get("date")), h.get("sourceTitle") or f"afsnit {ep}")
        if series:
            out.series_ids.add(int(series))
    for h in radarr.imports_since(start):
        movie = h.get("movieId")
        if not movie:
            continue
        add(f"movie:{int(movie)}", parse_date(h.get("date")), h.get("sourceTitle") or f"film {movie}")
        out.movie_ids.add(int(movie))
    return out


def bazarr_searches_by_itself(bazarr: Bazarr) -> str | None:
    """None hvis Bazarrs egen søgning efter manglende tekster står på "Manually" for serier og film, ellers grunden."""
    series, movies = bazarr.wanted_search_hours()
    wrong = [name for name, hours in (("serier", series), ("film", movies)) if hours != MANUALLY]
    if not wrong:
        return None
    return (
        "Bazarr søger selv efter manglende tekster til " + " og ".join(wrong) + " (Settings › Scheduler › "
        '"Search for Missing Subtitles" skal stå på Manually)'
    )


def ensure_profiles(cfg: Config, bazarr: Bazarr, new: NewImports) -> tuple[int, int, str | None]:
    """Giv nye serier/film uden sprogprofil profilen BAZARR_PROFILE.

    Returnerer (antal serier, antal film, advarsel). Advarslen er sat, når der ikke blev sat profiler af en grund,
    der bør rettes (profilen findes ikke, eller Bazarr søger selv efter manglende tekster).
    """
    if not new.series_ids and not new.movie_ids:
        return 0, 0, None
    series = [s for s in bazarr.series_by_ids(sorted(new.series_ids)) if s.get("profileId") in (None, 0, "")]
    movies = [m for m in bazarr.movies_by_ids(sorted(new.movie_ids)) if m.get("profileId") in (None, 0, "")]
    if not series and not movies:
        return 0, 0, None
    wanted = cfg.bazarr_profile.strip().lower()
    profile = next((p for p in bazarr.profiles() if str(p.get("name", "")).strip().lower() == wanted), None)
    if profile is None:
        return 0, 0, f"sprogprofilen {cfg.bazarr_profile!r} findes ikke i Bazarr (BAZARR_PROFILE i .env)"
    why = bazarr_searches_by_itself(bazarr)
    if why:
        return 0, 0, why + " - sætter ingen sprogprofiler, så Bazarr ikke søger til hele det gamle bibliotek"
    pid = int(profile["profileId"])
    done_s = done_m = 0
    for s in series:
        name = s.get("title") or f"serie {s.get('sonarrSeriesId')}"
        if cfg.dry_run:
            log.info("DRY_RUN: ville give %s sprogprofilen %s i Bazarr (nyt afsnit)", name, profile.get("name"))
            continue
        try:
            bazarr.set_series_profile(int(s["sonarrSeriesId"]), pid)
            log.info("Nyt: %s fik sprogprofilen %s i Bazarr", name, profile.get("name"))
            done_s += 1
        except ApiError as e:
            log.warning("Kunne ikke give %s sprogprofilen %s i Bazarr: %s", name, profile.get("name"), e)
    for m in movies:
        name = m.get("title") or f"film {m.get('radarrId')}"
        if cfg.dry_run:
            log.info("DRY_RUN: ville give %s sprogprofilen %s i Bazarr (ny film)", name, profile.get("name"))
            continue
        try:
            bazarr.set_movie_profile(int(m["radarrId"]), pid)
            log.info("Nyt: %s fik sprogprofilen %s i Bazarr", name, profile.get("name"))
            done_m += 1
        except ApiError as e:
            log.warning("Kunne ikke give %s sprogprofilen %s i Bazarr: %s", name, profile.get("name"), e)
    return done_s, done_m, None


MANUAL_DAYS = 30  # så længe gælder et valg med knappen (bagefter ryddes det op)


def add_manual(db, new: NewImports, now: datetime | None = None) -> int:
    """Trin C4: læg film/afsnit valgt med "Oversæt nu" eller "Prøv igen" i Fangarr oven i de nye importer.

    De behandles som nye (sprogprofil i Bazarr, valgt uden Seerr-ønske/mærke, og en tidligere fejl nulstilles,
    når knappen er trykket efter sidste behandling). Valg ældre end MANUAL_DAYS slettes.
    """
    now = now or datetime.now(UTC)
    db.conn.execute("DELETE FROM manual WHERE added_at < ?", ((now - timedelta(days=MANUAL_DAYS)).isoformat(),))
    db.conn.commit()
    count = 0
    for row in db.conn.execute("SELECT * FROM manual").fetchall():
        when = parse_date(row["added_at"])
        key = row["key"]
        if when is None:
            continue
        if key not in new.at or when > new.at[key]:
            new.at[key] = when
        new.manual.add(key)
        new.titles.setdefault(key, row["title"] or key)
        if row["media_type"] == "movie":
            new.movie_ids.add(int(row["media_id"]))
        elif row["series_id"]:
            new.series_ids.add(int(row["series_id"]))
        count += 1
    return count
