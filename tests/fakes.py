"""Falske Bazarr-, Radarr-, Sonarr- og Seerr-servere (httpx2.MockTransport)."""

from __future__ import annotations

import email
import email.policy
import os
import threading
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
        self.fail: dict[str, int | str] = {}  # vært -> HTTP-kode, "timeout" eller "hang" (svarer aldrig)
        self.release = threading.Event()  # sættes for at slippe kald, der "hænger"
        # Bazarrs tjenester, søgninger og synkronisering
        self.providers = [
            {"name": "opensubtitlescom", "status": "Good", "retry": "-"},
            {"name": "podnapisi", "status": "Good", "retry": "-"},
        ]
        self.search_calls: list[dict] = []
        self.search_job_status: str | None = "completed"  # status på søgejobbet; None = gammel Bazarr uden jobkø
        self.search_finds_danish: set[str] = set()  # "movie:1" / "episode:5" -> søgningen henter dansk
        self.on_search = None  # funktion(params) der fx sætter en tjeneste på pause
        self.sync_calls: list[str] = []
        self.bazarr_unknown: set[str] = set()  # "movie:1" / "series:50" -> Bazarr kender den ikke endnu
        self.radarr_tags = [{"id": 3, "label": "oversaet"}]
        self.uploads: list[dict] = []  # felter fra upload-kaldet + "query" og "content" (filens indhold)
        self.upload_status = 204
        self.upload_saves = True  # som Bazarr: gem den uploadede fil som <video>.da.srt
        self.scan_calls: list[dict] = []
        # Synk af den danske tekst mod lyden (PATCH /api/subtitles, action=sync)
        self.subtitle_syncs: list[dict] = []
        self.subtitle_sync_status = 204
        self.subtitle_sync_job: str | None = "completed"  # status på synkjobbet; None = intet job (gammel Bazarr)
        self.on_subtitle_sync = None  # funktion(form), fx til at notere rækkefølgen af kald
        # Trin B9: TMDB. "movie:101"/"tv:500" -> {"original_language": "en", "DK": {"flatrate": ["Netflix"]}}
        self.tmdb: dict[str, dict] = {}
        self.tmdb_tvdb: dict[int, int] = {}  # tvdbId -> TMDB-id (find)
        self.tmdb_calls: list[str] = []
        self._job_id = 100

    # --- opsætning ----------------------------------------------------------------
    def video(self, name: str) -> str:
        path = os.path.join(self.media_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"video")
        return path

    def add_movie(self, radarr_id, tmdb_id, added_ago, subtitles, requested=True, title=None, tags=()):
        path = self.video(f"Film {radarr_id}/Film {radarr_id}.mkv")
        self.movies[radarr_id] = {
            "title": title or f"Film {radarr_id}",
            "tmdbId": tmdb_id,
            "path": path,
            "subtitles": subtitles,
            "dateAdded": ago(**added_ago),
            "tags": list(tags),
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
        if host in self.fail:
            if self.fail[host] == "timeout":
                raise httpx2.ReadTimeout("timed out", request=req)
            if self.fail[host] == "hang":
                self.release.wait(30)
                return httpx2.Response(503)
            return httpx2.Response(self.fail[host], json={"error": "nej"})
        if host == "bazarr":
            assert req.headers.get("X-API-KEY") == "bazarr-key"
            return self.bazarr(req, path, q)
        if host == "radarr":
            return self.radarr(path)
        if host == "sonarr":
            return self.sonarr(path)
        if host == "tmdb":
            return self.tmdb_api(path, q)
        if host == "seerr":
            return httpx2.Response(200, json={"pageInfo": {"pages": 1}, "results": self.seerr_requests})
        return httpx2.Response(404)

    @staticmethod
    def missing(item) -> list[dict]:
        # Indlejret dansk tæller ikke her (som når Bazarr er sat til at ignorere indlejrede spor).
        if any(s.get("code2") == "da" and s.get("path") for s in item["subtitles"]):
            return []
        return [{"code2": "da", "forced": False, "hi": False}]

    def bazarr(self, req, path, q):
        if path == "/api/movies/wanted":
            data = [
                {"radarrId": i, "title": m["title"], "missing_subtitles": self.missing(m)}
                for i, m in self.movies.items()
                if self.missing(m) and f"movie:{i}" not in self.bazarr_unknown
            ]
            return httpx2.Response(200, json={"data": data, "total": len(data)})
        if path == "/api/episodes/wanted":
            data = [
                {
                    "sonarrEpisodeId": i,
                    "sonarrSeriesId": e["seriesId"],
                    "seriesTitle": e["title"],
                    "episode_number": f"{e['season']}x{i:02d}",
                    "missing_subtitles": self.missing(e),
                }
                for i, e in self.episodes.items()
                if self.missing(e) and f"series:{e['seriesId']}" not in self.bazarr_unknown and not e.get("new")
            ]
            return httpx2.Response(200, json={"data": data, "total": len(data)})
        if path == "/api/movies" and req.method == "GET":
            data = [
                {"radarrId": i, "path": m["path"], "subtitles": m["subtitles"], "missing_subtitles": self.missing(m)}
                for i, m in ((int(x), self.movies.get(int(x))) for x in q.get("radarrid[]", []))
                if m is not None and f"movie:{i}" not in self.bazarr_unknown
            ]
            return httpx2.Response(200, json={"data": data})
        if path == "/api/episodes":
            e = self.episodes[int(q["episodeid[]"][0])]
            data = {"path": e["path"], "subtitles": e["subtitles"], "missing_subtitles": self.missing(e)}
            return httpx2.Response(200, json={"data": [data]})
        if path == "/api/series" and req.method == "GET":
            data = []
            for x in q.get("seriesid[]", []):
                sid = int(x)
                if sid in self.series and f"series:{sid}" not in self.bazarr_unknown:
                    count = sum(1 for e in self.episodes.values() if e["seriesId"] == sid and not e.get("new"))
                    data.append({"sonarrSeriesId": sid, "episodeFileCount": count})
            return httpx2.Response(200, json={"data": data})
        if path == "/api/providers":
            return httpx2.Response(200, json={"data": self.providers})
        if path in ("/api/movies/subtitles", "/api/episodes/subtitles") and req.method == "PATCH":
            params = {k: v[0] for k, v in q.items()}
            self.search_calls.append({"path": path, **params})
            if path == "/api/movies/subtitles":
                key, item = f"movie:{params['radarrid']}", self.movies[int(params["radarrid"])]
            else:
                key, item = f"episode:{params['episodeid']}", self.episodes[int(params["episodeid"])]
            if key in self.search_finds_danish:
                da = os.path.splitext(item["path"])[0] + ".da.srt"
                with open(da, "w", encoding="utf-8") as f:
                    f.write("1\n00:00:01,000 --> 00:00:02,000\nHej\n")
                item["subtitles"] = [*item["subtitles"], {"code2": "da", "code3": "dan", "path": da, "forced": False}]
            if self.search_job_status is not None and self.jobs is not None:
                self._job_id += 1
                self.jobs.append(
                    {"job_id": self._job_id, "job_name": "Searching subtitles", "status": self.search_job_status}
                )
            if self.on_search:
                self.on_search(params)
            return httpx2.Response(204, text="")
        if path in ("/api/movies/subtitles", "/api/episodes/subtitles") and req.method == "POST":
            return self.upload(req, path, q)
        if path in ("/api/movies", "/api/series") and req.method == "PATCH":
            self.scan_calls.append({"path": path, **{k: v[0] for k, v in q.items()}})
            return httpx2.Response(204, text="")
        if path == "/api/system/tasks" and req.method == "POST":
            self.sync_calls.append(q["taskid"][0])
            return httpx2.Response(204, text="")
        if path == "/api/system/jobs":
            if self.jobs is None:
                return httpx2.Response(404)
            return httpx2.Response(200, json={"data": self.jobs})
        if path == "/api/subtitles" and req.method == "PATCH":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            if form.get("action") == "sync":
                return self.subtitle_sync(form)
            self.translate_calls.append(form)
            if self.on_translate:
                self.on_translate(form)
            return httpx2.Response(self.translate_status, text="")
        return httpx2.Response(404)

    def subtitle_sync(self, form):
        self.subtitle_syncs.append(form)
        if self.on_subtitle_sync:
            self.on_subtitle_sync(form)
        if self.subtitle_sync_status != 204:
            return httpx2.Response(self.subtitle_sync_status, text="nej")
        if self.subtitle_sync_job is not None and self.jobs is not None:
            self._job_id += 1
            self.jobs.append(
                {"job_id": self._job_id, "job_name": f"Syncing {form['path']}", "status": self.subtitle_sync_job}
            )
        return httpx2.Response(204, text="")

    def upload(self, req, path, q):
        head = f"Content-Type: {req.headers['content-type']}\r\n\r\n".encode()
        msg = email.message_from_bytes(head + req.content, policy=email.policy.HTTP)
        fields: dict = {"path": path, "query": {k: v[0] for k, v in q.items()}}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                fields["filename"] = part.get_filename()
                fields["content"] = part.get_payload(decode=True).decode("utf-8")
            else:
                fields[name] = part.get_content().strip()
        self.uploads.append(fields)
        if self.upload_status == 204 and self.upload_saves:
            if path == "/api/movies/subtitles":
                item = self.movies[int(q["radarrid"][0])]
            else:
                item = self.episodes[int(q["episodeid"][0])]
            da = os.path.splitext(item["path"])[0] + ".da.srt"
            with open(da, "w", encoding="utf-8") as f:
                f.write(fields["content"])
            item["subtitles"] = [*item["subtitles"], {"code2": "da", "code3": "dan", "path": da, "forced": False}]
        return httpx2.Response(self.upload_status, text="")

    def tmdb_api(self, path, q):
        self.tmdb_calls.append(path)
        assert q.get("api_key") == ["tmdb-key"]
        parts = path.strip("/").split("/")  # ["3", "movie", "101", ("watch", "providers")]
        if parts[1] == "find":
            tmdb_id = self.tmdb_tvdb.get(int(parts[2]))
            return httpx2.Response(200, json={"tv_results": [{"id": tmdb_id}] if tmdb_id else []})
        data = self.tmdb.get(f"{parts[1]}:{parts[2]}")
        if data is None:
            return httpx2.Response(404, json={"status_message": "not found"})
        if parts[3:] == ["watch", "providers"]:
            dk = {kind: [{"provider_name": n} for n in names] for kind, names in (data.get("DK") or {}).items()}
            return httpx2.Response(200, json={"id": int(parts[2]), "results": {"DK": dk} if dk else {}})
        return httpx2.Response(200, json={"id": int(parts[2]), "original_language": data.get("original_language")})

    def radarr(self, path):
        self.radarr_calls += 1

        def one(rid):
            m = self.movies[rid]
            return {
                "id": rid,
                "title": m["title"],
                "tmdbId": m["tmdbId"],
                "tags": m["tags"],
                "hasFile": True,
                "movieFile": {"path": m["path"], "dateAdded": m["dateAdded"]},
                **({"originalLanguage": m["originalLanguage"]} if "originalLanguage" in m else {}),
            }

        if path == "/api/v3/tag":
            return httpx2.Response(200, json=self.radarr_tags)
        if path == "/api/v3/movie":
            return httpx2.Response(200, json=[one(rid) for rid in self.movies])
        return httpx2.Response(200, json=one(int(path.rsplit("/", 1)[1])))

    def sonarr(self, path):
        if path == "/api/v3/tag":
            return httpx2.Response(200, json=self.tags)
        if path == "/api/v3/series":
            out = []
            for sid, s in self.series.items():
                count = sum(1 for e in self.episodes.values() if e["seriesId"] == sid)
                out.append({**s, "title": f"Serie {sid}", "statistics": {"episodeFileCount": count}})
            return httpx2.Response(200, json=out)
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
