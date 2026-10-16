"""Små klienter til Bazarr, Radarr, Sonarr og Seerr. Kun læsning - bortset fra Bazarrs translate."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx2 as httpx

from .config import Config


class ApiError(Exception):
    pass


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class _Api:
    def __init__(self, base_url: str, headers: dict[str, str], http: httpx.Client):
        self.base = base_url.rstrip("/")
        self.headers = headers
        self.http = http

    def get(self, path: str, params=None):
        r = self.http.get(self.base + path, params=params, headers=self.headers, timeout=60.0)
        if r.status_code >= 400:
            raise ApiError(f"GET {path} gav {r.status_code}")
        return r.json()


class Bazarr(_Api):
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

    def jobs(self) -> list[dict] | None:
        """Bazarrs jobkø (findes kun i nyere versioner). None hvis den ikke kan læses."""
        try:
            r = self.http.get(self.base + "/api/system/jobs", headers=self.headers, timeout=30.0)
            if r.status_code != 200:
                return None
            data = r.json()
            return data.get("data", data) if isinstance(data, dict) else data
        except (httpx.HTTPError, ValueError):
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
        return self.http.patch(self.base + "/api/subtitles", data=form, headers=self.headers, timeout=timeout)


class Radarr(_Api):
    def __init__(self, cfg: Config, http: httpx.Client):
        super().__init__(cfg.radarr_url, {"X-Api-Key": cfg.radarr_api_key}, http)

    def movie(self, radarr_id: int) -> dict:
        return self.get(f"/api/v3/movie/{radarr_id}")

    def movies(self) -> list[dict]:
        return self.get("/api/v3/movie")


class Sonarr(_Api):
    def __init__(self, cfg: Config, http: httpx.Client):
        super().__init__(cfg.sonarr_url, {"X-Api-Key": cfg.sonarr_api_key}, http)

    def episode(self, episode_id: int) -> dict:
        return self.get(f"/api/v3/episode/{episode_id}")

    def episode_file(self, file_id: int) -> dict:
        return self.get(f"/api/v3/episodefile/{file_id}")

    def series(self, series_id: int) -> dict:
        return self.get(f"/api/v3/series/{series_id}")

    def tags(self) -> list[dict]:
        return self.get("/api/v3/tag")


class Seerr(_Api):
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
