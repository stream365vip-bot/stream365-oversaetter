"""Falske Bazarr-, Radarr-, Sonarr- og Seerr-servere (httpx2.MockTransport)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx2


def ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


class FakeArr:
    def __init__(self, media_dir: str):
        self.media_dir = media_dir
        self.movies: dict[int, dict] = {}  # radarrId -> data
        self.episodes: dict[int, dict] = {}  # sonarrEpisodeId -> data
        self.series: dict[int, dict] = {}
        self.tags = [{"id": 1, "label": "andet"}, {"id": 7, "label": "oversaet"}]
        self.seerr_requests: list[dict] = []
        self.translate_calls: list[dict] = []
        self.radarr_calls = 0
        self.sonarr_episode_calls = 0
        self.translate_status = 204
        self.on_translate = None  # funktion(form) der fx skriver den danske fil
        self.jobs: list[dict] | None = []
        self.writes: list[tuple[str, str]] = []  # (metode, sti) for alt der ikke er GET

    # --- opsætning ----------------------------------------------------------------
    def video(self, name: str) -> str:
        path = os.path.join(self.media_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"video")
        return path

    def add_movie(self, radarr_id, tmdb_id, added_ago, subtitles, requested=True, title=None):
        path = self.video(f"Film {radarr_id}/Film {radarr_id}.mkv")
        self.movies[radarr_id] = {
            "title": title or f"Film {radarr_id}",
            "tmdbId": tmdb_id,
            "path": path,
            "subtitles": subtitles,
            "dateAdded": ago(**added_ago),
        }
        if requested:
            self.seerr_requests.append(
                {"type": "movie", "status": 2, "media": {"tmdbId": tmdb_id, "mediaType": "movie"}}
            )
        return path

    def add_episode(self, ep_id, series_id, tvdb_id, added_ago, subtitles, tags=(), season=1):
        path = self.video(f"Serie {series_id}/Season {season}/Serie {series_id} S{season:02d}E{ep_id:02d}.mkv")
        self.series[series_id] = {"id": series_id, "tvdbId": tvdb_id, "tmdbId": None, "tags": list(tags)}
        self.episodes[ep_id] = {
            "seriesId": series_id,
            "season": season,
            "path": path,
            "subtitles": subtitles,
            "dateAdded": ago(**added_ago),
            "title": f"Serie {series_id}",
        }
        return path

    # --- HTTP ---------------------------------------------------------------------
    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle))

    def handle(self, req: httpx2.Request) -> httpx2.Response:
        host, path = req.url.host, req.url.path
        q = parse_qs(str(req.url.query, "ascii") if isinstance(req.url.query, bytes) else str(req.url.query))
        if req.method != "GET":
            self.writes.append((req.method, f"{host}{path}"))
        if host == "bazarr":
            assert req.headers.get("X-API-KEY") == "bazarr-key"
            return self.bazarr(req, path, q)
        if host == "radarr":
            return self.radarr(path)
        if host == "sonarr":
            return self.sonarr(path)
        if host == "seerr":
            return httpx2.Response(200, json={"pageInfo": {"pages": 1}, "results": self.seerr_requests})
        return httpx2.Response(404)

    def bazarr(self, req, path, q):
        missing = [{"code2": "da", "forced": False, "hi": False}]
        if path == "/api/movies/wanted":
            data = [{"radarrId": i, "title": m["title"], "missing_subtitles": missing} for i, m in self.movies.items()]
            return httpx2.Response(200, json={"data": data, "total": len(data)})
        if path == "/api/episodes/wanted":
            data = [
                {
                    "sonarrEpisodeId": i,
                    "sonarrSeriesId": e["seriesId"],
                    "seriesTitle": e["title"],
                    "episode_number": f"{e['season']}x{i:02d}",
                    "missing_subtitles": missing,
                }
                for i, e in self.episodes.items()
            ]
            return httpx2.Response(200, json={"data": data, "total": len(data)})
        if path == "/api/movies":
            m = self.movies[int(q["radarrid[]"][0])]
            return httpx2.Response(200, json={"data": [{"path": m["path"], "subtitles": m["subtitles"]}]})
        if path == "/api/episodes":
            e = self.episodes[int(q["episodeid[]"][0])]
            return httpx2.Response(200, json={"data": [{"path": e["path"], "subtitles": e["subtitles"]}]})
        if path == "/api/system/jobs":
            if self.jobs is None:
                return httpx2.Response(404)
            return httpx2.Response(200, json={"data": self.jobs})
        if path == "/api/subtitles" and req.method == "PATCH":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.translate_calls.append(form)
            if self.on_translate:
                self.on_translate(form)
            return httpx2.Response(self.translate_status, text="")
        return httpx2.Response(404)

    def radarr(self, path):
        self.radarr_calls += 1

        def one(rid):
            m = self.movies[rid]
            return {
                "id": rid,
                "tmdbId": m["tmdbId"],
                "hasFile": True,
                "movieFile": {"path": m["path"], "dateAdded": m["dateAdded"]},
            }

        if path == "/api/v3/movie":
            return httpx2.Response(200, json=[one(rid) for rid in self.movies])
        return httpx2.Response(200, json=one(int(path.rsplit("/", 1)[1])))

    def sonarr(self, path):
        if path == "/api/v3/tag":
            return httpx2.Response(200, json=self.tags)
        kind, ident = path.split("/")[-2:]
        ident = int(ident)
        if kind == "episode":
            self.sonarr_episode_calls += 1
            e = self.episodes[ident]
            return httpx2.Response(
                200,
                json={
                    "id": ident,
                    "seriesId": e["seriesId"],
                    "seasonNumber": e["season"],
                    "episodeFileId": 1000 + ident,
                    "hasFile": True,
                },
            )
        if kind == "episodefile":
            e = self.episodes[ident - 1000]
            return httpx2.Response(200, json={"id": ident, "path": e["path"], "dateAdded": e["dateAdded"]})
        if kind == "series":
            return httpx2.Response(200, json=self.series[ident])
        return httpx2.Response(404)


def en_sub(path: str, sub_id: int = 1, hi: bool = False, forced: bool = False) -> dict:
    return {
        "code2": "en",
        "code3": "eng",
        "path": path,
        "forced": forced,
        "hi": hi,
        "embedded_track_id": None,
        "id": sub_id,
    }


def embedded(lang: str, sub_id: int, track: int = 2) -> dict:
    return {
        "code2": lang,
        "code3": "",
        "path": None,
        "forced": False,
        "hi": False,
        "embedded_track_id": track,
        "id": sub_id,
    }
