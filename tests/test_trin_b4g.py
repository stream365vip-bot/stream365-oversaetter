"""Trin B4g (18.10.2026): bed Plex scanne mappen, når den danske tekst er uploadet til Bazarr."""

import logging
import os
import threading
from urllib.parse import parse_qs

import httpx2
import pytest

from oversaetter import plex
from oversaetter.plex import find_section, map_path, refresh_folder
from tests.fakes import FakeArr
from tests.test_direct import FakeAnthropic, movie_with_english, runner
from tests.test_selection import make_cfg

TOKEN = "plex-token-123"


class FakePlex:
    def __init__(self, locations: dict[str, list[str]]):
        self.locations = locations  # bibliotekets nøgle -> mapper
        self.calls: list[tuple[str, dict, str | None]] = []  # (sti, query, X-Plex-Token)
        self.fail: int | str | None = None  # HTTP-kode, "connect" eller "hang"
        self.release = threading.Event()

    def handle(self, req: httpx2.Request) -> httpx2.Response:
        q = parse_qs(str(req.url.query, "ascii") if isinstance(req.url.query, bytes) else str(req.url.query))
        self.calls.append((req.url.path, {k: v[0] for k, v in q.items()}, req.headers.get("X-Plex-Token")))
        if self.fail == "connect":
            raise httpx2.ConnectError("nope", request=req)
        if self.fail == "hang":
            self.release.wait(30)
        elif self.fail:
            return httpx2.Response(self.fail, text="nej")
        if req.url.path == "/library/sections":
            dirs = [
                {"key": key, "title": f"Bibliotek {key}", "Location": [{"id": i, "path": p} for i, p in enumerate(ps)]}
                for key, ps in self.locations.items()
            ]
            return httpx2.Response(200, json={"MediaContainer": {"size": len(dirs), "Directory": dirs}})
        if req.url.path.endswith("/refresh"):
            return httpx2.Response(200)
        return httpx2.Response(404)

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle))


def cfg_with_plex(tmp_path, token=TOKEN, **kw):
    token_file = tmp_path / "plex_token.txt"
    token_file.write_text(token + "\n", encoding="utf-8")
    settings = dict(plex_url="http://plex:32400", plex_token_file=str(token_file))
    settings.update(kw)
    return make_cfg(tmp_path, **settings)


# --- rene funktioner -----------------------------------------------------------------------------------------
def test_section_is_found_from_location():
    sections = [
        {"key": "1", "Location": [{"path": "/mnt/unionfs/Media/Movies"}]},
        {"key": "2", "Location": [{"path": "/mnt/unionfs/Media/TV"}, {"path": "/mnt/unionfs/Media/TV4K/"}]},
        {"key": "3", "Location": [{"path": "/mnt/unionfs/Media/Movies/Børn"}]},
    ]
    assert find_section(sections, "/mnt/unionfs/Media/Movies/Idiots (2026)") == "1"
    assert find_section(sections, "/mnt/unionfs/Media/TV4K/Serie/Season 1") == "2"
    assert find_section(sections, "/mnt/unionfs/Media/Movies/Børn/Bamse (2020)") == "3"  # mest præcise sti vinder
    assert find_section(sections, "/mnt/unionfs/Media/Movies2/X") is None  # ikke bare samme begyndelse
    assert find_section(sections, "/andet/sted") is None


def test_path_map_swaps_prefix():
    assert map_path("/mnt/unionfs/Media/Movies/X", "/mnt/unionfs=/data") == "/data/Media/Movies/X"
    assert map_path("/mnt/unionfs/Media/Movies/X", "/mnt/unionfs/=/data/") == "/data/Media/Movies/X"
    assert map_path("/mnt/unionfsX/Media", "/mnt/unionfs=/data") == "/mnt/unionfsX/Media"
    assert map_path("/andet/X", "/mnt/unionfs=/data") == "/andet/X"
    assert map_path("/mnt/unionfs/Media", "") == "/mnt/unionfs/Media"


# --- refresh_folder ------------------------------------------------------------------------------------------
def test_refresh_gets_right_path_and_token_header(tmp_path, caplog):
    fake = FakePlex({"1": ["/mnt/unionfs/Media/TV"], "2": ["/mnt/unionfs/Media/Movies"]})
    cfg = cfg_with_plex(tmp_path)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        ok = refresh_folder(cfg, fake.client(), "/mnt/unionfs/Media/Movies/Idiots (2026)/Idiots (2026).mkv")
    assert ok is True
    assert fake.calls == [
        ("/library/sections", {}, TOKEN),
        ("/library/sections/2/refresh", {"path": "/mnt/unionfs/Media/Movies/Idiots (2026)"}, TOKEN),
    ]
    assert "Plex: bad om scanning af /mnt/unionfs/Media/Movies/Idiots (2026)" in caplog.text
    assert TOKEN not in caplog.text


def test_path_map_is_used_for_matching_and_refresh(tmp_path):
    fake = FakePlex({"5": ["/data/Media/Movies"]})
    cfg = cfg_with_plex(tmp_path, plex_path_map="/mnt/unionfs=/data")
    assert refresh_folder(cfg, fake.client(), "/mnt/unionfs/Media/Movies/X (2020)/X.mkv") is True
    assert fake.calls[-1][:2] == ("/library/sections/5/refresh", {"path": "/data/Media/Movies/X (2020)"})


@pytest.mark.parametrize("url,token", [("", TOKEN), ("http://plex:32400", "")])
def test_skipped_without_url_or_token(tmp_path, caplog, url, token):
    fake = FakePlex({"1": ["/mnt/unionfs/Media/Movies"]})
    cfg = cfg_with_plex(tmp_path, token=token, plex_url=url)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert refresh_folder(cfg, fake.client(), "/mnt/unionfs/Media/Movies/X/X.mkv") is None
    assert fake.calls == []
    assert len([r for r in caplog.records if r.message.startswith("Plex:")]) == 1


@pytest.mark.parametrize("fail", [401, 500, "connect"])
def test_plex_error_gives_warning_not_exception(tmp_path, caplog, fail):
    fake = FakePlex({"1": ["/mnt/unionfs/Media/Movies"]})
    fake.fail = fail
    cfg = cfg_with_plex(tmp_path)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert refresh_folder(cfg, fake.client(), "/mnt/unionfs/Media/Movies/X/X.mkv") is False
    warning = next(r for r in caplog.records if r.levelname == "WARNING")
    assert warning.message.startswith("Plex: kunne ikke bede om scanning af /mnt/unionfs/Media/Movies/X")
    assert TOKEN not in caplog.text


def test_unknown_folder_gives_warning(tmp_path, caplog):
    fake = FakePlex({"1": ["/mnt/unionfs/Media/TV"]})
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert refresh_folder(cfg_with_plex(tmp_path), fake.client(), "/mnt/unionfs/Media/Movies/X/X.mkv") is False
    assert "intet bibliotek i Plex indeholder mappen" in caplog.text
    assert [c[0] for c in fake.calls] == ["/library/sections"]  # ingen refresh


def test_hanging_plex_hits_hard_timeout(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(plex, "PLEX_TIMEOUT", 0.2)
    fake = FakePlex({"1": ["/mnt/unionfs/Media/Movies"]})
    fake.fail = "hang"
    try:
        with caplog.at_level(logging.INFO, logger="oversaetter"):
            assert refresh_folder(cfg_with_plex(tmp_path), fake.client(), "/mnt/unionfs/Media/Movies/X/X.mkv") is False
    finally:
        fake.release.set()
    assert "Plex: intet svar inden for 0.2 sekunder" in caplog.text


# --- hele flowet ---------------------------------------------------------------------------------------------
@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def with_plex(arr, fake):
    handle = arr.handle
    arr.handle = lambda req: fake.handle(req) if req.url.host == "plex" else handle(req)


def test_upload_asks_plex_and_saves_column(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    fake = FakePlex({"1": [str(tmp_path / "Media")]})
    with_plex(arr, fake)
    token_file = tmp_path / "plex_token.txt"
    token_file.write_text(TOKEN, encoding="utf-8")
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), plex_url="http://plex", plex_token_file=str(token_file))
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["plex_refresh"] == 1
    assert fake.calls[-1] == ("/library/sections/1/refresh", {"path": os.path.dirname(video)}, TOKEN)


def test_plex_failure_does_not_fail_translation(arr, tmp_path):
    movie_with_english(arr)
    fake = FakePlex({"1": [str(tmp_path / "Media")]})
    fake.fail = 503
    with_plex(arr, fake)
    token_file = tmp_path / "plex_token.txt"
    token_file.write_text(TOKEN, encoding="utf-8")
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), plex_url="http://plex", plex_token_file=str(token_file))
    r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["plex_refresh"] == 0


def test_without_plex_url_column_stays_empty(arr, tmp_path):
    movie_with_english(arr)
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["plex_refresh"] is None


def test_failed_upload_does_not_ask_plex(arr, tmp_path):
    movie_with_english(arr)
    arr.upload_status = 500
    fake = FakePlex({"1": [str(tmp_path / "Media")]})
    with_plex(arr, fake)
    token_file = tmp_path / "plex_token.txt"
    token_file.write_text(TOKEN, encoding="utf-8")
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), plex_url="http://plex", plex_token_file=str(token_file))
    r.run_once()
    assert fake.calls == [] and db.get("movie:1")["plex_refresh"] is None
