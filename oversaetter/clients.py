"""Små klienter til Bazarr, Radarr, Sonarr og Seerr.

Kun læsning - bortset fra tre kald til Bazarr: translate, søg efter dansk til ét emne og synkronisér
med Radarr/Sonarr. Ingen af dem ændrer indstillinger.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx2 as httpx

from .config import Config
from .deadline import call_with_timeout


class ApiError(Exception):
    """Fejl fra en tjeneste. Teksten er dansk og indeholder aldrig API-nøgler."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


HINTS = {
    401: "forkert API-nøgle i .env",
    403: "forkert API-nøgle i .env",
    404: "forkert adresse i .env, eller en for gammel version",
}
GET_TIMEOUT = 60.0
JOBS_TIMEOUT = 30.0
# Ekstra sekunder oven i httpx' timeout, før kaldet opgives helt (httpx' timeout gælder kun hver enkelt læsning).
HARD_MARGIN = 5.0


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class _Api:
    name = "Tjenesten"

    def __init__(self, base_url: str, headers: dict[str, str], http: httpx.Client):
        self.base = base_url.rstrip("/")
        self.headers = headers
        self.http = http

    def get(self, path: str, params=None):
        r = self._request("GET", path, params, GET_TIMEOUT)
        try:
            return r.json()
        except ValueError:
            raise ApiError(
                f"{self.name} svarede med HTTP {r.status_code} på {path}, men ikke med gyldige data - "
                "tjek adressen i .env",
                r.status_code,
            ) from None

    def _request(self, method: str, path: str, params, timeout: float) -> httpx.Response:
        # Fejlteksterne bygges selv, så hverken headers (nøgler) eller httpx' egne beskeder kommer i loggen.
        try:
            r = call_with_timeout(
                self.http.request,
                timeout + HARD_MARGIN,
                method,
                self.base + path,
                params=params,
                headers=self.headers,
                timeout=httpx.Timeout(timeout),
            )
        except (httpx.TimeoutException, TimeoutError):
            raise ApiError(
                f"{self.name} svarede ikke inden for {timeout:g} sekunder (timeout) på {self.base}{path} - "
                "tjek adressen i .env, og at tjenesten kører"
            ) from None
        except httpx.HTTPError as e:
            raise ApiError(
                f"{self.name}: kunne ikke forbinde til {self.base}{path} ({type(e).__name__}) - "
                "tjek adressen i .env, og at tjenesten kører"
            ) from None
        if r.status_code >= 400:
            hint = HINTS.get(r.status_code, "fejl i tjenesten" if r.status_code >= 500 else "uventet svar")
            raise ApiError(f"{self.name} svarede med HTTP {r.status_code} på {path} ({hint})", r.status_code)
        return r


class Bazarr(_Api):
    name = "Bazarr"

    def __init__(self, cfg: Config, http: httpx.Client):
        super().__init__(cfg.bazarr_url, {"X-API-KEY": cfg.bazarr_api_key}, http)

    def wanted_movies(self) -> list[dict]:
        return self.get("/api/movies/wanted", {"start": 0, "length": -1}).get("data", [])

    def wanted_episodes(self) -> list[dict]:
        return self.get("/api/episodes/wanted", {"start": 0, "length": -1}).get("data", [])

    def movie(self, radarr_id: int) -> dict | None:
        data = self.get("/api/movies", {"radarrid[]": radarr_id}).get("data", [])
        return data[0] if data else None

    def episode(self, episode_id: int) -> dict | None:
        data = self.get("/api/episodes", {"episodeid[]": episode_id}).get("data", [])
        return data[0] if data else None

    def item(self, media_type: str, media_id: int) -> dict | None:
        return self.movie(media_id) if media_type == "movie" else self.episode(media_id)

    def movies_by_ids(self, radarr_ids: list[int]) -> list[dict]:
        out = []
        for i in range(0, len(radarr_ids), 50):
            out += self.get("/api/movies", {"radarrid[]": radarr_ids[i : i + 50]}).get("data", [])
        return out

    def series_by_ids(self, series_ids: list[int]) -> list[dict]:
        out = []
        for i in range(0, len(series_ids), 50):
            out += self.get("/api/series", {"seriesid[]": series_ids[i : i + 50]}).get("data", [])
        return out

    def providers(self) -> list[dict]:
        """Status for Bazarrs aktiverede undertekst-tjenester: name, status ("Good" eller grunden) og retry."""
        return self.get("/api/providers").get("data", [])

    def search(
        self,
        media_type: str,
        media_id: int,
        series_id: int | None = None,
        hi: bool = False,
        timeout_s: float = 600,
        language: str = "da",
    ) -> None:
        """Bed Bazarr søge efter dansk (eller engelsk) til netop dette emne (samme kald som søgeknappen i Bazarr).

        I nyere versioner lægges søgningen i Bazarrs jobkø, og kaldet svarer med det samme.
        """
        params = {"language": language, "forced": "False", "hi": "True" if hi else "False"}
        if media_type == "movie":
            path = "/api/movies/subtitles"
            params["radarrid"] = media_id
        else:
            path = "/api/episodes/subtitles"
            params.update(seriesid=series_id, episodeid=media_id)
        self._request("PATCH", path, params, timeout_s)

    def sync(self, task_id: str) -> None:
        """Start Bazarrs opgave "Sync with Radarr" (update_movies) eller "Sync with Sonarr" (update_series)."""
        self._request("POST", "/api/system/tasks", {"taskid": task_id}, GET_TIMEOUT)

    def jobs(self) -> list[dict] | None:
        """Bazarrs jobkø (findes kun i nyere versioner). None hvis den ikke kan læses."""
        try:
            r = call_with_timeout(
                self.http.get,
                JOBS_TIMEOUT + HARD_MARGIN,
                self.base + "/api/system/jobs",
                headers=self.headers,
                timeout=JOBS_TIMEOUT,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            return data.get("data", data) if isinstance(data, dict) else data
        except (httpx.HTTPError, ValueError, TimeoutError):
            return None

    def translate(
        self, media_type: str, media_id: int, timeout_s: float, path: str | None = None, subtitles_id: int | None = None
    ) -> httpx.Response:
        """Det eneste kald der ændrer noget: bed Bazarr oversætte til dansk."""
        form = {"action": "translate", "language": "da", "type": media_type, "id": str(media_id)}
        if path:
            form["path"] = path
        if subtitles_id is not None:
            form["subtitles_id"] = str(subtitles_id)
        timeout = httpx.Timeout(timeout_s, connect=30.0)
        try:
            return call_with_timeout(
                self.http.patch,
                timeout_s + HARD_MARGIN,
                self.base + "/api/subtitles",
                data=form,
                headers=self.headers,
                timeout=timeout,
            )
        except TimeoutError:
            # Samme vej som httpx' egen timeout: request_translation tjekker, om filen alligevel er kommet.
            raise httpx.ReadTimeout(f"Bazarr svarede ikke inden for {timeout_s:g} sekunder") from None


class Radarr(_Api):
    name = "Radarr"

    def __init__(self, cfg: Config, http: httpx.Client):
        super().__init__(cfg.radarr_url, {"X-Api-Key": cfg.radarr_api_key}, http)

    def movie(self, radarr_id: int) -> dict:
        return self.get(f"/api/v3/movie/{radarr_id}")

    def movies(self) -> list[dict]:
        return self.get("/api/v3/movie")

    def tags(self) -> list[dict]:
        return self.get("/api/v3/tag")


class Sonarr(_Api):
    name = "Sonarr"

    def __init__(self, cfg: Config, http: httpx.Client):
        super().__init__(cfg.sonarr_url, {"X-Api-Key": cfg.sonarr_api_key}, http)

    def episode(self, episode_id: int) -> dict:
        return self.get(f"/api/v3/episode/{episode_id}")

    def episode_file(self, file_id: int) -> dict:
        return self.get(f"/api/v3/episodefile/{file_id}")

    def series(self, series_id: int) -> dict:
        return self.get(f"/api/v3/series/{series_id}")

    def all_series(self) -> list[dict]:
        return self.get("/api/v3/series")

    def tags(self) -> list[dict]:
        return self.get("/api/v3/tag")


class Seerr(_Api):
    name = "Seerr"

    def __init__(self, cfg: Config, http: httpx.Client):
        super().__init__(cfg.seerr_url, {"X-Api-Key": cfg.seerr_api_key}, http)
        self.enabled = bool(cfg.seerr_url)

    def requests(self) -> list[dict]:
        if not self.enabled:
            return []
        out, skip, take = [], 0, 100
        while True:
            data = self.get("/api/v1/request", {"take": take, "skip": skip, "filter": "all"})
            results = data.get("results", [])
            out.extend(results)
            pages = (data.get("pageInfo") or {}).get("pages", 1)
            skip += take
            if not results or skip >= pages * take:
                return out


class SeerrIndex:
    """Hvad er ønsket via Seerr: film (tmdbId) og serier (tvdbId/tmdbId) med evt. sæsoner."""

    def __init__(self, requests: list[dict]):
        self.movies: set[int] = set()
        self.series_tvdb: dict[int, set[int] | None] = {}
        self.series_tmdb: dict[int, set[int] | None] = {}
        for req in requests:
            if req.get("status") == 3:  # afvist
                continue
            media = req.get("media") or {}
            kind = req.get("type") or media.get("mediaType")
            if kind == "movie":
                if media.get("tmdbId"):
                    self.movies.add(int(media["tmdbId"]))
                continue
            seasons = {int(s["seasonNumber"]) for s in req.get("seasons") or [] if "seasonNumber" in s}
            for key, target in (("tvdbId", self.series_tvdb), ("tmdbId", self.series_tmdb)):
                if media.get(key):
                    sid = int(media[key])
                    if not seasons or target.get(sid, set()) is None:
                        target[sid] = None  # hele serien
                    else:
                        target[sid] = (target.get(sid) or set()) | seasons

    def series_maybe_wanted(self, tvdb_id: int | None, tmdb_id: int | None) -> bool:
        return bool((tvdb_id and int(tvdb_id) in self.series_tvdb) or (tmdb_id and int(tmdb_id) in self.series_tmdb))

    def movie_wanted(self, tmdb_id: int | None) -> bool:
        return bool(tmdb_id) and int(tmdb_id) in self.movies

    def episode_wanted(self, tvdb_id: int | None, tmdb_id: int | None, season: int | None) -> bool:
        for sid, target in ((tvdb_id, self.series_tvdb), (tmdb_id, self.series_tmdb)):
            if sid and int(sid) in target:
                seasons = target[int(sid)]
                if seasons is None or season in seasons:
                    return True
        return False
