"""Trin B6b (20.10.2026): rettelser efter Clean Hands - aldrig HI ved upload, og synk mod lyden før Plex.

Alle tekster er selvskrevne.
"""

import logging
import os

import pytest

from oversaetter import clients, synk
from oversaetter.config import Config
from tests.fakes import FakeArr, embedded, en_sub
from tests.test_direct import EN, FakeAnthropic, FakeCommands, movie_with_english, runner, stream
from tests.test_trin_b4g import TOKEN, FakePlex, with_plex


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def plex_runner(arr, tmp_path, wrap=None, **kw):
    """Runner med Plex. wrap(handle) -> handle lægges uden om både Bazarr og Plex, før klienten oprettes."""
    fake = FakePlex({"1": [str(tmp_path / "Media")]})
    with_plex(arr, fake)
    if wrap:
        arr.handle = wrap(arr.handle)
    token_file = tmp_path / "plex_token.txt"
    token_file.write_text(TOKEN, encoding="utf-8")
    r, db = runner(arr, tmp_path, plex_url="http://plex", plex_token_file=str(token_file), **kw)
    return r, db, fake


def recorder(seen):
    """Notér (vært, metode, sti) for alle kald, der ændrer noget (Bazarr) eller går til Plex."""

    def wrap(handle):
        def wrapped(req):
            if req.method != "GET" or req.url.host == "plex":
                seen.append((req.url.host, req.method, req.url.path))
            return handle(req)

        return wrapped

    return wrap


# --- 1) den danske tekst uploades altid uden HI -------------------------------------------------------------
def test_embedded_english_sdh_track_gives_upload_without_hi(arr, tmp_path):
    """Clean Hands: indlejret engelsk SDH-spor (ffmpeg) -> dansk upload med hi=false og navnet .da.srt."""
    video = arr.add_movie(1, 101, {"days": 2}, [{**embedded("en", 11225, track=3), "hi": True}])
    commands = FakeCommands(streams=[stream(3, title="English [SDH]", hi=1)])
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), commands=commands)
    r.run_once()
    assert [c[0] for c in commands.calls] == ["ffprobe", "ffmpeg"]
    assert db.get("movie:1")["en_hi"] == 1
    up = arr.uploads[0]
    assert (up["language"], up["forced"], up["hi"]) == ("da", "false", "false")
    assert up["filename"] == "Film 1.da.srt"
    sync = arr.subtitle_syncs[0]
    assert (sync["language"], sync["forced"], sync["hi"]) == ("da", "False", "False")
    assert sync["path"] == os.path.splitext(video)[0] + ".da.srt"


def test_downloaded_english_hi_srt_gives_upload_without_hi(arr, tmp_path):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.hi.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.movies[1]["subtitles"] = [en_sub(en_path, hi=True)]
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig"
    up = arr.uploads[0]
    assert up["hi"] == "false" and up["forced"] == "false" and up["filename"] == "Film 1.da.srt"
    assert arr.subtitle_syncs[0]["hi"] == "False"


def test_episode_upload_and_sync_without_hi(arr, tmp_path):
    video = arr.add_episode(5, 50, 500, {"days": 2}, [], tags=[7])
    en_path = os.path.splitext(video)[0] + ".en.sdh.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[5]["subtitles"] = [en_sub(en_path, hi=True)]
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert db.get("episode:5")["status"] == "faerdig"
    assert arr.uploads[0]["hi"] == "false"
    assert arr.uploads[0]["query"] == {"seriesid": "50", "episodeid": "5"}
    sync = arr.subtitle_syncs[0]
    assert (sync["type"], sync["id"], sync["hi"]) == ("episode", "5", "False")
    assert db.get("episode:5")["bazarr_sync"] == 1


def test_warning_when_bazarr_registers_danish_as_hi(arr, tmp_path, caplog):
    movie_with_english(arr)
    handle = arr.handle

    def wrapped(req):
        resp = handle(req)
        if req.method == "POST":  # lige efter uploaden: Bazarr har registreret teksten som HI
            for s in arr.movies[1]["subtitles"]:
                if s.get("code2") == "da":
                    s["hi"] = True
        return resp

    arr.handle = wrapped
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert "Bazarr har registreret den danske tekst til Film 1 som HI" in caplog.text
    assert db.get("movie:1")["status"] == "faerdig"


# --- 2) synk efter upload, før Plex ---------------------------------------------------------------------------
def test_sync_is_called_after_upload_and_before_plex(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    seen = []
    r, db, fake = plex_runner(arr, tmp_path, wrap=recorder(seen), claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    order = [(h, m, p) for h, m, p in seen if p != "/api/movies/subtitles" or m == "POST"]
    assert order == [
        ("bazarr", "POST", "/api/movies/subtitles"),
        ("bazarr", "PATCH", "/api/subtitles"),
        ("plex", "GET", "/library/sections"),
        ("plex", "GET", "/library/sections/1/refresh"),
    ]
    sync = arr.subtitle_syncs[0]
    assert sync == {
        "action": "sync",
        "language": "da",
        "path": os.path.splitext(video)[0] + ".da.srt",
        "type": "movie",
        "id": "1",
        "forced": "False",
        "hi": "False",
    }
    row = db.get("movie:1")
    assert row["bazarr_sync"] == 1 and row["plex_refresh"] == 1
    assert "Bazarr har synkroniseret den danske tekst til Film 1 mod lyden" in caplog.text


def test_waits_for_sync_job_before_plex(arr, tmp_path):
    movie_with_english(arr)
    arr.subtitle_sync_job = "running"
    polls = []

    def wrap(handle):
        def wrapped(req):
            if req.url.path == "/api/system/jobs" and arr.subtitle_syncs:
                polls.append(1)
                if len(polls) == 3:  # jobbet bliver færdigt efter et par opslag
                    arr.jobs[-1]["status"] = "completed"
            if req.url.host == "plex":
                assert arr.jobs[-1]["status"] == "completed", "Plex må først kaldes, når synken er færdig"
            return handle(req)

        return wrapped

    r, db, fake = plex_runner(arr, tmp_path, wrap=wrap, claude=FakeAnthropic())
    r.run_once()
    assert len(polls) >= 3 and fake.calls
    assert db.get("movie:1")["bazarr_sync"] == 1


def test_bazarr_without_sync_job_counts_as_synced(arr, tmp_path):
    movie_with_english(arr)
    arr.subtitle_sync_job = None  # gammel Bazarr: synken er færdig, når kaldet svarer
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert db.get("movie:1")["bazarr_sync"] == 1


@pytest.mark.parametrize("fail", [500, "job"])
def test_sync_failure_is_warned_and_plex_still_called(arr, tmp_path, caplog, fail):
    movie_with_english(arr)
    if fail == "job":
        arr.subtitle_sync_job = "failed"
    else:
        arr.subtitle_sync_status = fail
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["bazarr_sync"] == 0 and row["plex_refresh"] == 1
    warning = next(x for x in caplog.records if x.levelname == "WARNING")
    assert warning.message.startswith("ADVARSEL: Bazarr")
    assert "Film 1" in warning.message and "ikke synkroniseret" in warning.message
    assert fake.calls[-1][0] == "/library/sections/1/refresh"


def test_sync_timeout_is_warned_and_plex_still_called(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.subtitle_sync_job = "running"  # bliver aldrig færdig
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["bazarr_sync"] == 0 and row["plex_refresh"] == 1
    assert "har ikke synkroniseret den danske tekst til Film 1 inden for 10 minutter" in caplog.text
    assert fake.calls[-1][0] == "/library/sections/1/refresh"


def test_hanging_sync_call_hits_timeout(arr, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(synk, "SYNC_TIMEOUT", 0.2)
    monkeypatch.setattr(clients, "HARD_MARGIN", 0.2)
    movie_with_english(arr)
    arr.on_subtitle_sync = lambda form: arr.release.wait(30)
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic())
    try:
        with caplog.at_level(logging.INFO, logger="oversaetter"):
            r.run_once()
    finally:
        arr.release.set()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["bazarr_sync"] == 0 and row["plex_refresh"] == 1
    assert "ADVARSEL: Bazarr kunne ikke synkronisere den danske tekst til Film 1" in caplog.text


def test_sync_after_upload_false_skips_sync(arr, tmp_path, caplog):
    movie_with_english(arr)
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic(), sync_after_upload=False)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert arr.subtitle_syncs == []
    row = db.get("movie:1")
    assert row["bazarr_sync"] is None and row["plex_refresh"] == 1
    assert "SYNC_AFTER_UPLOAD=false" in caplog.text


def test_failed_upload_does_not_sync(arr, tmp_path):
    movie_with_english(arr)
    arr.upload_status = 500
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert arr.subtitle_syncs == [] and db.get("movie:1")["bazarr_sync"] is None


def test_config_switch():
    assert Config.from_env({}).sync_after_upload is True
    assert Config.from_env({"SYNC_AFTER_UPLOAD": "false"}).sync_after_upload is False
    assert Config.from_env({"SYNC_AFTER_UPLOAD": "true"}).sync_after_upload is True
