"""Filmkontekst til systeminstruksen (CONTEXT=on): titel, år, genre, aldersgrænse, sprog, handling og roller.

Kilder: først Radarr/Sonarr (ingen ny nøgle). Findes en TMDB-nøgle, hentes også dansk handling og rolleliste med
køn fra TMDB. Fejl i et opslag stopper aldrig oversættelsen: så oversættes der uden kontekst (med en WARNING).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .clients import _Api

log = logging.getLogger("oversaetter")

MAX_CAST = 15
MAX_PLOT = 1200
GENDERS = {1: "female", 2: "male", 3: "non-binary"}


@dataclass
class FilmContext:
    title: str = ""
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    certification: str = ""
    language: str = ""
    country: str = ""
    overview: str = ""
    cast: list[tuple[str, str, str]] = field(default_factory=list)  # (skuespiller, rolle, køn)
    # Kun serier
    series: bool = False
    season: int | None = None
    episode: int | None = None
    episode_title: str = ""
    episode_overview: str = ""

    def block(self) -> str:
        """Teksten til systeminstruksen (tom, hvis der intet er at sige)."""
        if not (self.title or self.overview or self.cast):
            return ""
        kind = "Series" if self.series else "Film"
        lines = [f"{kind} context (for understanding only - never translate or return it):"]
        if self.title:
            lines.append(f"Title: {self.title}" + (f" ({self.year})" if self.year else ""))
        if self.series and (self.season is not None or self.episode_title):
            ep = "Episode" if self.season is None else f"Season {self.season}"
            if self.season is not None and self.episode is not None:
                ep += f" episode {self.episode}"
            lines.append(ep + (f': "{self.episode_title}"' if self.episode_title else ""))
        if self.genres:
            lines.append("Genre: " + ", ".join(self.genres))
        if self.certification:
            lines.append(f"Age rating: {self.certification}")
        if self.language or self.country:
            lines.append("Original language/country: " + " / ".join(x for x in (self.language, self.country) if x))
        if self.overview:
            lines.append(("Series plot: " if self.series else "Plot: ") + _short(self.overview))
        if self.episode_overview:
            lines.append("Episode plot: " + _short(self.episode_overview))
        if self.cast:
            lines.append("Cast (actor - character - gender):")
            lines += [f"- {a} - {c or '?'} - {g or 'unknown'}" for a, c, g in self.cast[:MAX_CAST]]
        return "\n".join(lines)


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_PLOT else text[: MAX_PLOT - 3].rstrip() + "..."


def _lang(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def from_radarr(movie: dict) -> FilmContext:
    return FilmContext(
        title=str(movie.get("title") or ""),
        year=movie.get("year") or None,
        genres=[str(g) for g in movie.get("genres") or []],
        certification=str(movie.get("certification") or ""),
        language=_lang(movie.get("originalLanguage")),
        overview=str(movie.get("overview") or ""),
    )


def from_sonarr(series: dict, episode: dict) -> FilmContext:
    return FilmContext(
        title=str(series.get("title") or ""),
        year=series.get("year") or None,
        genres=[str(g) for g in series.get("genres") or []],
        certification=str(series.get("certification") or ""),
        language=_lang(series.get("originalLanguage")),
        overview=str(series.get("overview") or ""),
        series=True,
        season=episode.get("seasonNumber"),
        episode=episode.get("episodeNumber"),
        episode_title=str(episode.get("title") or ""),
        episode_overview=str(episode.get("overview") or ""),
    )


class Tmdb(_Api):
    name = "TMDB"

    def __init__(self, base_url: str, key: str, http):
        # En v4-læsenøgle (JWT, starter med "eyJ") sendes som Bearer; en v3-nøgle som api_key (aldrig i loggen).
        self.bearer = key.startswith("eyJ")
        headers = {"Authorization": f"Bearer {key}"} if self.bearer else {}
        super().__init__(base_url, headers, http)
        self.key = key

    def _get(self, path: str, **params):
        if not self.bearer:
            params["api_key"] = self.key
        return self.get(path, params)

    def movie(self, tmdb_id: int, language: str = "da-DK") -> dict:
        return self._get(f"/movie/{tmdb_id}", language=language, append_to_response="credits,release_dates")

    def tv(self, tmdb_id: int, language: str = "da-DK") -> dict:
        return self._get(f"/tv/{tmdb_id}", language=language, append_to_response="credits,content_ratings")

    def tv_episode(self, tmdb_id: int, season: int, episode: int, language: str = "da-DK") -> dict:
        return self._get(f"/tv/{tmdb_id}/season/{season}/episode/{episode}", language=language)


def _cast(data: dict) -> list[tuple[str, str, str]]:
    people = sorted((data.get("credits") or {}).get("cast") or [], key=lambda p: p.get("order", 999))
    return [
        (str(p.get("name") or ""), str(p.get("character") or ""), GENDERS.get(p.get("gender"), ""))
        for p in people[:MAX_CAST]
        if p.get("name")
    ]


def _certification(data: dict) -> str:
    """Aldersgrænse fra TMDB: dansk, ellers amerikansk."""
    by_country: dict[str, str] = {}
    for r in (data.get("release_dates") or {}).get("results") or []:
        cert = next((d.get("certification") for d in r.get("release_dates") or [] if d.get("certification")), "")
        if cert:
            by_country[r.get("iso_3166_1", "")] = cert
    for r in (data.get("content_ratings") or {}).get("results") or []:
        if r.get("rating"):
            by_country[r.get("iso_3166_1", "")] = r["rating"]
    return by_country.get("DK") or by_country.get("US") or ""


def add_tmdb(ctx: FilmContext, tmdb: Tmdb, tmdb_id: int) -> FilmContext:
    """Læg dansk handling, rolleliste med køn, land og evt. manglende felter fra TMDB oven i konteksten."""
    if ctx.series:
        data = tmdb.tv(tmdb_id)
        title, date = data.get("name"), data.get("first_air_date")
        countries = data.get("origin_country") or []
    else:
        data = tmdb.movie(tmdb_id)
        title, date = data.get("title"), data.get("release_date")
        countries = [c.get("iso_3166_1") for c in data.get("production_countries") or [] if c.get("iso_3166_1")]
    ctx.title = ctx.title or str(data.get("original_title") or data.get("original_name") or title or "")
    if not ctx.year and date and str(date)[:4].isdigit():
        ctx.year = int(str(date)[:4])
    if not ctx.genres:
        ctx.genres = [str(g.get("name")) for g in data.get("genres") or [] if g.get("name")]
    ctx.certification = ctx.certification or _certification(data)
    ctx.language = ctx.language or str(data.get("original_language") or "")
    ctx.country = ctx.country or ", ".join(str(c) for c in countries)
    if data.get("overview"):
        ctx.overview = str(data["overview"])  # dansk handling vinder over Radarr/Sonarrs engelske
    elif not ctx.overview:
        en = tmdb.tv(tmdb_id, "en-US") if ctx.series else tmdb.movie(tmdb_id, "en-US")
        ctx.overview = str(en.get("overview") or "")
    ctx.cast = _cast(data) or ctx.cast
    if ctx.series and ctx.season is not None and ctx.episode is not None:
        try:
            ep = tmdb.tv_episode(tmdb_id, ctx.season, ctx.episode)
            ctx.episode_title = str(ep.get("name") or "") or ctx.episode_title
            ctx.episode_overview = str(ep.get("overview") or "") or ctx.episode_overview
        except Exception as e:  # afsnittet findes måske ikke hos TMDB endnu
            log.debug("TMDB kender ikke afsnittet %sx%s: %s", ctx.season, ctx.episode, e)
    return ctx


def safe(build, title: str) -> str | None:
    """Kør build() og returnér kontekstblokken - eller None med en WARNING, hvis noget fejler."""
    try:
        text = build()
    except Exception as e:  # aldrig lade konteksten stoppe oversættelsen
        log.warning(
            "Filmkontekst til %s kunne ikke hentes (%s: %s) - oversætter uden kontekst", title, type(e).__name__, e
        )
        return None
    if not text:
        log.warning("Ingen filmkontekst fundet til %s - oversætter uden kontekst", title)
        return None
    log.info("Filmkontekst til %s: %d tegn", title, len(text))
    return text


def for_candidate(cand, radarr, sonarr, tmdb: Tmdb | None) -> str | None:
    """Kontekstblokken for et emne i driften (film fra Radarr, afsnit fra Sonarr, evt. suppleret fra TMDB)."""

    def build() -> str:
        if cand.media_type == "movie":
            movie = radarr.movie(cand.media_id)
            ctx, tmdb_id = from_radarr(movie), movie.get("tmdbId")
        else:
            episode = sonarr.episode(cand.media_id)
            series = sonarr.series(cand.series_id or episode.get("seriesId"))
            ctx, tmdb_id = from_sonarr(series, episode), series.get("tmdbId")
        if tmdb is not None and tmdb_id:
            try:
                add_tmdb(ctx, tmdb, int(tmdb_id))
            except Exception as e:
                log.warning("TMDB-opslag fejlede for %s (%s) - bruger kun Radarr/Sonarr", cand.title, e)
        return ctx.block()

    return safe(build, cand.title)


def for_tmdb_id(tmdb: Tmdb, ident: str) -> str | None:
    """Testtilstanden: kontekst kun fra TMDB. ident er "123" (film) eller "tv:123" (serie)."""
    series = ident.lower().startswith("tv:")
    tmdb_id = int(ident.split(":", 1)[1] if series else ident)
    return safe(lambda: add_tmdb(FilmContext(series=series), tmdb, tmdb_id).block(), f"TMDB {ident}")
