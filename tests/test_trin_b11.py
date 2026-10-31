"""Trin B11 (31.10.2026): serverstatus til appens backend (STATUS_URL) hvert 5. minut.

Alle tekster og tal er selvskrevne.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import httpx2
import pytest

from oversaetter import status
from oversaetter.config import Config
from oversaetter.db import Database, iso
from oversaetter.status import StatusJob
from tests.fakes import FakeArr
from tests.helpers import cfg as base_cfg
from tests.test_direct import FakeAnthropic, movie_with_english, runner

STATUS = "http://status.test/status/feeder"
TOKEN = "feed-token-123"


class FakeServers:
    """Falske tjenester til status-jobbet: Radarr, Sonarr, Seerr, Bazarr, qBittorrent, Plex og backenden."""

    def __init__(self, backend: int | str = 201):
        self.backend = backend
        self.posts: list[dict] = []
        self.calls: list[str] = []
        self.hang: set[str] = set()
        self.release = threading.Event()

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle))

    def handle(self, req: httpx2.Request) -> httpx2.Response:
        host, path = req.url.host, req.url.path
        self.calls.append(f"{req.method} {host}{path}")
        if host in self.hang:
            self.release.wait(30)
            return httpx2.Response(503)
        if host == "status.test":
            self.posts.append({"json": json.loads(req.content), "token": req.headers.get("X-Oversaetter-Token")})
            if self.backend == "connect":
                raise httpx2.ConnectError("nope", request=req)
            return httpx2.Response(self.backend, json={})
        if host == "dockerproxy" and path == "/containers/json":
            assert req.method == "GET"
            running = ("plex", "bazarr", "radarr", "qbittorrent", "autoscan", "seerr")
            out = [{"Names": [f"/{n}"], "State": "running"} for n in running]
            out.append({"Names": ["/sonarr2"], "State": "exited"})
            return httpx2.Response(200, json=out)
        if host == "radarr" and path == "/api/v3/queue":
            assert req.headers["X-Api-Key"] == "radarr-key"
            recs = [{"status": "downloading"}, {"status": "warning"}, {"status": "completed"}]
            return httpx2.Response(200, json={"totalRecords": 3, "records": recs})
        if host == "sonarr" and path == "/api/v3/queue":
            recs = [{"status": "failed"}, {"status": "downloading", "trackedDownloadStatus": "warning"}]
            return httpx2.Response(200, json={"totalRecords": 5, "records": recs})
        if host == "seerr" and path == "/api/v1/request":
            assert req.url.params["filter"] == "pending"
            return httpx2.Response(200, json={"pageInfo": {"pages": 4, "results": 4}, "results": [{}]})
        if host == "bazarr" and path == "/api/movies/wanted":
            return httpx2.Response(200, json={"data": [{}], "total": 12})
        if host == "bazarr" and path == "/api/episodes/wanted":
            return httpx2.Response(200, json={"data": [{}], "total": 34})
        if host == "qbit" and path == "/api/v2/auth/login":
            assert req.content == b"username=admin&password=hemmelig%3Akode"
            return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "SID=abc; HttpOnly; path=/"})
        if host == "qbit" and path == "/api/v2/transfer/info":
            assert "SID=abc" in req.headers.get("Cookie", "")
            return httpx2.Response(200, json={"dl_info_speed": 12_500_000})  # 100 Mbit/s
        if host == "plex" and path == "/status/sessions":
            assert req.headers["X-Plex-Token"] == "plex-token"
            sessions = [
                {"Session": {"bandwidth": 8000}, "TranscodeSession": {"videoDecision": "transcode"}},
                {"Session": {"bandwidth": 20500}},
            ]
            return httpx2.Response(200, json={"MediaContainer": {"size": 2, "Metadata": sessions}})
        return httpx2.Response(404)


def fake_proc(tmp_path) -> str:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 1 1 1\n")
    (proc / "meminfo").write_text("MemTotal:  1000 kB\nMemFree:  100 kB\nMemAvailable:  250 kB\n")
    (proc / "loadavg").write_text("1.50 1.20 0.90 2/300 12345\n")
    (proc / "uptime").write_text(f"{86400 * 3.5:.2f} 1000.00\n")
    return str(proc)


def make_cfg(tmp_path, **kw) -> Config:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "feed_token.txt").write_text(TOKEN + "\n")
    (secrets / "plex_token.txt").write_text("plex-token\n")
    (secrets / "qbittorrent.txt").write_text("admin:hemmelig:kode\n")
    union = tmp_path / "unionfs"
    (union / "Media").mkdir(parents=True, exist_ok=True)
    (union / "Media" / "kendt.txt").write_text("ja")
    settings = dict(
        data_dir=str(tmp_path / "data"),
        status_url=STATUS,
        feed_token_file=str(secrets / "feed_token.txt"),
        radarr_url="http://radarr",
        radarr_api_key="radarr-key",
        sonarr_url="http://sonarr",
        sonarr_api_key="sonarr-key",
        seerr_url="http://seerr",
        seerr_api_key="seerr-key",
        bazarr_url="http://bazarr",
        bazarr_api_key="bazarr-key",
        qbittorrent_url="http://qbit",
        qbittorrent_file=str(secrets / "qbittorrent.txt"),
        plex_url="http://plex",
        plex_token_file=str(secrets / "plex_token.txt"),
        status_disk_path=str(tmp_path),
        status_unionfs_path=str(union),
        status_docker="http://dockerproxy:2375",
        status_plex=True,
        monthly_budget_usd=20.0,
    )
    settings.update(kw)
    return base_cfg(**settings)


def fill_db(c: Config) -> None:
    db = Database(os.path.join(c.data_dir, "oversaetter.db"))
    db.add_spend("movie:1", 10, 10, 1.25)
    db.add_spend("movie:2", 10, 10, 0.5)
    last_month = (datetime.now().replace(day=1) - timedelta(days=2)).strftime("%Y-%m")
    db.conn.execute("INSERT INTO spend (ts, month, cost_usd) VALUES (?, ?, ?)", (iso(), last_month, 9.0))
    rows = [
        ("movie:1", "venter_paa_dansk", iso()),
        ("movie:2", "venter_paa_dansk", iso()),
        ("movie:3", "mangler_engelsk", iso()),
        ("movie:4", "fejlet", iso()),
        ("movie:5", "fejlet", f"{last_month}-15T10:00:00+00:00"),
        ("movie:6", "faerdig", iso()),
    ]
    for key, st, updated in rows:
        db.upsert(key, media_type="movie", media_id=int(key[6:]), title=key, status=st)
        db.conn.execute("UPDATE items SET updated_at = ? WHERE key = ?", (updated, key))
    db.conn.commit()


class FakeRunner:
    def __init__(self):
        self.t = 100.0
        self.current = None
        self.pause_until = None
        self.pause_at = None
        self.batch_since = None
        self.queue = 0

    def clock(self):
        return self.t


def job(tmp_path, servers=None, runner_=None, **kw):
    c = make_cfg(tmp_path, **kw)
    servers = servers or FakeServers()
    j = StatusJob(
        c,
        runner_ if runner_ is not None else FakeRunner(),
        http=servers.client(),
        proc_dir=fake_proc(tmp_path),
    )
    j.cpu_prev = (0, 0)  # undgå 1 sekunds måling i testene
    return j, servers, c


# --- alle felter ---------------------------------------------------------------------------------
def test_all_fields_are_sent_with_token(tmp_path, caplog):
    r = FakeRunner()
    r.current = ("movie:9", "Ny film")
    r.queue = 3
    j, servers, c = job(tmp_path, runner_=r)
    fill_db(c)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert j.run_once() is True
    assert len(servers.posts) == 1
    post = servers.posts[0]
    assert post["token"] == TOKEN
    p = post["json"]
    assert isinstance(p["disk_free_gb"], float) and isinstance(p["unionfs_free_gb"], float)
    assert p["mount_ok"] is True
    assert p["containers_down"] == ["sonarr2"]
    # /proc/stat: 1000 i alt, 800 ledig (idle + iowait) siden (0, 0) -> 20 %
    assert p["cpu_pct"] == 20.0 and p["ram_pct"] == 75.0 and p["load"] == 1.5 and p["uptime_days"] == 3.5
    assert p["radarr_queue"] == 3 and p["sonarr_queue"] == 5 and p["failed_downloads"] == 3
    assert p["download_speed_mbps"] == 100.0
    assert p["seerr_pending"] == 4
    assert p["bazarr_wanted_movies"] == 12 and p["bazarr_wanted_episodes"] == 34 and p["bazarr_no_english"] == 1
    assert p["plex_streams"] == 2 and p["plex_transcodes"] == 1 and p["plex_bandwidth_mbps"] == 28.5
    assert p["translator"] == {
        "state": "i_gang",
        "title": "Ny film",
        "queue": 3,
        "waiting_for_danish": 2,
        "failed": 1,
        "month_usd": 1.75,
        "budget_usd": 20.0,
    }
    assert TOKEN not in caplog.text and "plex-token" not in caplog.text and "hemmelig" not in caplog.text
    assert "Status: første statusbesked sendt" in caplog.text
    # Intet skrives i databasen og intet uden for GET/login/POST til backenden
    assert all(call.startswith("GET ") for call in servers.calls if "status.test" not in call and "login" not in call)


def test_translator_states(tmp_path):
    r = FakeRunner()
    j, _, _ = job(tmp_path, runner_=r)
    assert j.translator()["state"] == "ledig" and "title" not in j.translator()
    assert "month_usd" not in j.translator()  # ingen database endnu: feltet udelades
    r.current = ("movie:1", "Film")
    r.batch_since = time.time() - 9 * 60 - 5
    t = j.translator()
    assert t["state"] == "venter" and t["batch_wait_min"] == 9 and t["title"] == "Film"
    r.pause_until = r.t + 600
    r.pause_at = datetime(2026, 10, 31, 17, 30).astimezone()
    t = j.translator()
    assert t["state"] == "pause" and t["pause_until"].startswith("2026-10-31T17:30:00")
    r.t += 601  # pausen er slut (runnerens eget ur)
    assert j.translator()["state"] == "venter"


def test_mount_not_readable_gives_false(tmp_path):
    j, servers, _ = job(tmp_path, status_mount_file=str(tmp_path / "findes-ikke.mkv"))
    j.run_once()
    assert servers.posts[0]["json"]["mount_ok"] is False


# --- qBittorrent ikke sat ------------------------------------------------------------------------
def test_qbittorrent_not_set_is_left_out_and_logged_once(tmp_path, caplog):
    j, servers, _ = job(tmp_path, qbittorrent_url="")
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        j.run_once()
        j.run_once()
    for post in servers.posts:
        assert "download_speed_mbps" not in post["json"] and "radarr_queue" in post["json"]
    lines = [rec for rec in caplog.records if "download_speed_mbps udelades" in rec.message]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert not any("qbit" in call for call in servers.calls)


def test_qbittorrent_wrong_password_is_left_out(tmp_path, caplog):
    servers = FakeServers()
    handle = servers.handle

    def wrong(req):
        if req.url.host == "qbit":
            return httpx2.Response(200, text="Fails.")
        return handle(req)

    servers.handle = wrong
    j, _, _ = job(tmp_path, servers=servers)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        j.run_once()
    assert "download_speed_mbps" not in servers.posts[0]["json"]
    assert "qBittorrent afviser bruger/kode" in caplog.text and "hemmelig" not in caplog.text


# --- ingen docker-socket-proxy --------------------------------------------------------------------
def test_no_docker_proxy_leaves_out_containers_down(tmp_path, caplog):
    j, servers, _ = job(tmp_path, status_docker="")
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        j.run_once()
        j.run_once()
    assert all("containers_down" not in post["json"] for post in servers.posts)
    assert "cpu_pct" in servers.posts[0]["json"]
    lines = [rec for rec in caplog.records if "containers_down udelades" in rec.message]
    assert [r.message for r in lines] == [
        "Status: containers_down udelades - STATUS_DOCKER er tom (ingen docker-socket-proxy sat op)"
    ]
    assert lines[0].levelno == logging.INFO
    assert not any("dockerproxy" in call for call in servers.calls)


def test_docker_socket_path_is_refused(tmp_path, caplog):
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    j, servers, _ = job(tmp_path, status_docker=str(sock))
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        j.run_once()
    assert "containers_down" not in servers.posts[0]["json"]
    assert "er ikke en http-adresse til en docker-socket-proxy" in caplog.text


def test_plex_fields_off_by_default(tmp_path, caplog):
    j, servers, _ = job(tmp_path, status_plex=False)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        j.run_once()
    assert not any(k.startswith("plex_") for k in servers.posts[0]["json"])
    assert not any("plex" in call for call in servers.calls)
    assert "Status: plex udelades - STATUS_PLEX=false" in caplog.text


def test_docker_proxy_url_is_used(tmp_path):
    servers = FakeServers()
    handle = servers.handle
    servers.handle = lambda req: (
        httpx2.Response(200, json=[{"Names": ["/plex"], "State": "running"}])
        if req.url.host == "dockerproxy" and req.url.path == "/containers/json"
        else handle(req)
    )
    j, _, _ = job(tmp_path, servers=servers, status_docker="http://dockerproxy:2375/")
    j.run_once()
    assert servers.posts[0]["json"]["containers_down"] == [
        "bazarr",
        "radarr",
        "sonarr2",
        "qbittorrent",
        "autoscan",
        "seerr",
    ]


# --- fejl og tidsgrænser -------------------------------------------------------------------------
def test_backend_500_gives_one_warning_and_recovers(tmp_path, caplog):
    servers = FakeServers(backend=500)
    j, _, _ = job(tmp_path, servers=servers)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert j.run_once() is False
        assert j.run_once() is False
        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert [w.message for w in warnings] == ["Status: POST udelades - backenden svarede med HTTP 500"]
        servers.backend = 201
        assert j.run_once() is True
    assert "Status: sendt igen" in caplog.text
    assert len(servers.posts) == 3  # ingen genforsøg: ét POST pr. runde


def test_hanging_service_is_left_out_within_deadline(tmp_path, caplog):
    servers = FakeServers()
    servers.hang.add("radarr")
    j, _, _ = job(tmp_path, servers=servers)
    try:
        with caplog.at_level(logging.INFO, logger="oversaetter"):
            started = time.monotonic()
            payload = j.collect(deadline=0.5)
            assert time.monotonic() - started < 3
            assert "radarr_queue" not in payload and "sonarr_queue" in payload
            assert payload["failed_downloads"] == 2  # kun Sonarr nåede at svare
            # Næste runde: Radarr hænger stadig - der startes ikke en ny tråd
            j.collect(deadline=0.5)
        assert "radarr_queue udelades - intet svar inden for 0.5 sekunder" in caplog.text
        assert "radarr_queue udelades - forrige måling hænger stadig" in caplog.text
        assert sum("radarr" in call for call in servers.calls) == 1
    finally:
        servers.release.set()


def test_service_401_is_left_out_with_reason(tmp_path, caplog):
    servers = FakeServers()
    handle = servers.handle
    servers.handle = lambda req: httpx2.Response(401) if req.url.host == "seerr" else handle(req)
    j, _, _ = job(tmp_path, servers=servers)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        j.run_once()
    assert "seerr_pending" not in servers.posts[0]["json"]
    assert "Seerr svarede med HTTP 401 på /api/v1/request (forkert API-nøgle?)" in caplog.text


def test_no_token_sends_nothing(tmp_path, caplog):
    j, servers, c = job(tmp_path)
    open(c.feed_token_file, "w").close()
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert j.run_once() is False
    assert servers.posts == []
    assert "intet token i" in caplog.text


def test_status_url_empty_starts_nothing(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert status.start(make_cfg(tmp_path, status_url=""), FakeRunner()) is None
    assert "STATUS_URL er tom" in caplog.text


def test_config_reads_new_keys():
    c = Config.from_env(
        {
            "STATUS_URL": " https://x.test/status/feeder ",
            "STATUS_CONTAINERS": "plex,bazarr",
            "QBITTORRENT_URL": "http://qbittorrent:8080/",
            "STATUS_PLEX": "false",
        }
    )
    assert c.status_url == "https://x.test/status/feeder" and c.status_containers == "plex,bazarr"
    assert c.qbittorrent_url == "http://qbittorrent:8080" and c.status_plex is False
    d = Config.from_env({})
    assert d.status_url == "" and d.status_containers == "plex,bazarr,radarr,sonarr2,qbittorrent,autoscan,seerr"
    assert d.qbittorrent_file == "/run/secrets/qbittorrent" and d.status_docker == "" and d.status_plex is False


def test_database_is_opened_read_only(tmp_path):
    j, _, c = job(tmp_path)
    fill_db(c)
    path = os.path.join(c.data_dir, "oversaetter.db")
    before = os.path.getmtime(path)
    j.translator()
    assert os.path.getmtime(path) == before
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(f"file:{path}?mode=ro", uri=True).execute("DELETE FROM items")


# --- driften er upåvirket --------------------------------------------------------------------------
def test_translation_unaffected_while_status_job_fails(tmp_path, caplog):
    arr = FakeArr(str(tmp_path / "Media"))
    movie_with_english(arr)
    claude = FakeAnthropic()
    r, db = runner(arr, tmp_path, claude=claude)
    servers = FakeServers(backend=500)
    servers.hang.add("plex")
    c = make_cfg(tmp_path)
    j = StatusJob(c, r, http=servers.client(), proc_dir=str(tmp_path / "ingen-proc"))
    j.cpu_prev = (0, 0)
    try:
        with caplog.at_level(logging.INFO, logger="oversaetter"):
            worker = threading.Thread(target=lambda: j.collect(deadline=1.0) and j.send({"x": 1}), daemon=True)
            worker.start()
            r.run_once()
            worker.join(5)
            assert j.run_once() is False
        assert db.get("movie:1")["status"] == "faerdig"
        assert len(arr.uploads) == 1
        assert r.queue == 0 and r.batch_since is None and r.current is None
        assert "backenden svarede med HTTP 500" in caplog.text
        assert "cpu/ram/load/uptime udelades" in caplog.text  # /proc mangler: feltet udelades, intet vælter
    finally:
        servers.release.set()


def test_runner_tracks_queue_and_batch_wait(tmp_path):
    arr = FakeArr(str(tmp_path / "Media"))
    r, _ = runner(arr, tmp_path)
    r._batch_saved("msgbatch_1")
    first = r.batch_since
    assert first is not None
    r._batch_saved("msgbatch_1")
    assert r.batch_since == first
    r._batch_saved(None)
    assert r.batch_since is None
