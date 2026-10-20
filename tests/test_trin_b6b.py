"""Trin B6b (20.10.2026): rettelser efter Clean Hands.

Den danske tekst uploades aldrig som HI. Bazarr synkroniserer selv ved upload: vi venter på Bazarrs job, tjekker
derefter HI-mærket og beder først så Plex scanne. Vores egen synk (SYNC_AFTER_UPLOAD) er slået fra som standard.

Alle tekster er selvskrevne.
"""

import json
import logging
import os

import pytest

from oversaetter import clients, hi, synk
from oversaetter.config import Config
from tests.fakes import FakeArr, embedded, en_sub
from tests.helpers import cue, srt
from tests.test_direct import (
    DANISH,
    EN,
    FakeAnthropic,
    FakeCommands,
    movie_with_english,
    payload,
    reply,
    runner,
    stream,
)
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


def bazarr_sync_finishes(arr, after_polls=3, mark_hi=False, plex_check=True):
    """Bazarrs egen synk efter upload bliver færdig efter nogle opslag i joblisten (og kan omdøbe til .da.hi.srt)."""
    polls = []

    def finish():
        job = next(j for j in arr.jobs if j["job_name"].startswith("Syncing "))
        job["status"] = "completed"
        if mark_hi:
            for s in arr.movies[1]["subtitles"]:
                if s.get("code2") == "da":
                    hi_path = s["path"].replace(".da.srt", ".da.hi.srt")
                    os.replace(s["path"], hi_path)
                    s.update(path=hi_path, hi=True)

    def wrap(handle):
        def wrapped(req):
            if req.url.path == "/api/system/jobs" and arr.uploads:
                polls.append(1)
                if len(polls) == after_polls:
                    finish()
            if plex_check and req.url.host == "plex":
                assert len(polls) >= after_polls, "Plex må først kaldes, når Bazarrs synk er færdig"
            return handle(req)

        return wrapped

    return wrap, polls


# --- 1) den danske tekst uploades altid uden HI -------------------------------------------------------------
def test_embedded_english_sdh_track_gives_upload_without_hi(arr, tmp_path):
    """Clean Hands: indlejret engelsk SDH-spor (ffmpeg) -> dansk upload med hi=false og navnet .da.srt."""
    arr.add_movie(1, 101, {"days": 2}, [{**embedded("en", 11225, track=3), "hi": True}])
    commands = FakeCommands(streams=[stream(3, title="English [SDH]", hi=1)])
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), commands=commands)
    r.run_once()
    assert [c[0] for c in commands.calls] == ["ffprobe", "ffmpeg"]
    assert db.get("movie:1")["en_hi"] == 1
    up = arr.uploads[0]
    assert (up["language"], up["forced"], up["hi"]) == ("da", "false", "false")
    assert up["filename"] == "Film 1.da.srt"


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


def test_episode_upload_without_hi(arr, tmp_path):
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


# --- 2) vent på Bazarrs egen synk, så HI-tjek, så Plex ------------------------------------------------------
def test_default_no_own_sync_but_waits_for_bazarr_before_plex(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.upload_sync_job = "running"
    wrap, polls = bazarr_sync_finishes(arr)
    r, db, fake = plex_runner(arr, tmp_path, wrap=wrap, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert arr.subtitle_syncs == []  # standard: ingen egen synk
    row = db.get("movie:1")
    assert row["bazarr_sync"] is None and row["plex_refresh"] == 1
    assert len(polls) >= 3 and fake.calls
    assert "Bazarr er færdig med upload/synk af den danske tekst til Film 1" in caplog.text
    assert "ADVARSEL" not in caplog.text


def test_hi_check_runs_after_bazarr_sync_and_names_file(arr, tmp_path, caplog):
    """Bazarr registrerer først teksten som HI, når dens egen synk er færdig: tjekket skal vente på det."""
    video, _ = movie_with_english(arr)
    arr.upload_sync_job = "running"
    wrap, _ = bazarr_sync_finishes(arr, mark_hi=True)
    r, db, fake = plex_runner(arr, tmp_path, wrap=wrap, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    warning = next(x for x in caplog.records if x.levelname == "WARNING")
    assert warning.message.startswith("ADVARSEL: Bazarr har registreret den danske tekst til Film 1 som HI")
    assert "gemt som Film 1.da.hi.srt" in warning.message
    assert os.path.splitext(video)[0] + ".da.hi.srt" in warning.message
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["plex_refresh"] == 1


def test_no_hi_warning_for_plain_danish(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.upload_sync_job = "completed"
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert "som HI" not in caplog.text and db.get("movie:1")["status"] == "faerdig"


def test_bazarr_sync_timeout_warns_checks_hi_and_calls_plex(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.upload_sync_job = "running"  # bliver aldrig færdig
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert "Bazarrs egen synk af den danske tekst til Film 1 er ikke færdig efter 10 minutter" in caplog.text
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["plex_refresh"] == 1
    assert fake.calls[-1][0] == "/library/sections/1/refresh"


def test_job_for_other_title_is_not_waited_for(arr, tmp_path):
    movie_with_english(arr)
    arr.jobs.append({"job_id": 1, "job_name": "Syncing /x/Film 10/Film 10.da.srt", "status": "running"})
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig"
    assert r.clock() < synk.BAZARR_WAIT  # ventede ikke på "Film 10"


def test_unreadable_job_list_does_not_block(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.jobs = None  # gammel Bazarr uden jobkø
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert "Bazarrs jobliste kan ikke læses" in caplog.text
    assert db.get("movie:1")["plex_refresh"] == 1


def test_failed_upload_does_not_wait_or_sync(arr, tmp_path):
    movie_with_english(arr)
    arr.upload_status = 500
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), sync_after_upload=True)
    r.run_once()
    assert arr.subtitle_syncs == [] and db.get("movie:1")["bazarr_sync"] is None


# --- 3) vores egen synk, kun med SYNC_AFTER_UPLOAD=true -----------------------------------------------------
def test_own_sync_after_bazarr_and_before_plex(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    seen = []
    r, db, fake = plex_runner(arr, tmp_path, wrap=recorder(seen), claude=FakeAnthropic(), sync_after_upload=True)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    order = [(h, m, p) for h, m, p in seen if p != "/api/movies/subtitles" or m == "POST"]
    assert order == [
        ("bazarr", "POST", "/api/movies/subtitles"),
        ("bazarr", "PATCH", "/api/subtitles"),
        ("plex", "GET", "/library/sections"),
        ("plex", "GET", "/library/sections/1/refresh"),
    ]
    assert arr.subtitle_syncs[0] == {
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


def test_own_sync_for_episode(arr, tmp_path):
    video = arr.add_episode(5, 50, 500, {"days": 2}, [], tags=[7])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[5]["subtitles"] = [en_sub(en_path)]
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), sync_after_upload=True)
    r.run_once()
    sync = arr.subtitle_syncs[0]
    assert (sync["type"], sync["id"], sync["hi"], sync["forced"]) == ("episode", "5", "False", "False")
    assert db.get("episode:5")["bazarr_sync"] == 1


def test_own_sync_waits_for_its_job(arr, tmp_path):
    movie_with_english(arr)
    arr.subtitle_sync_job = "running"
    polls = []

    def wrap(handle):
        def wrapped(req):
            if req.url.path == "/api/system/jobs" and arr.subtitle_syncs:
                polls.append(1)
                if len(polls) == 3:
                    arr.jobs[-1]["status"] = "completed"
            if req.url.host == "plex":
                assert arr.jobs[-1]["status"] == "completed", "Plex må først kaldes, når synken er færdig"
            return handle(req)

        return wrapped

    r, db, fake = plex_runner(arr, tmp_path, wrap=wrap, claude=FakeAnthropic(), sync_after_upload=True)
    r.run_once()
    assert len(polls) >= 3 and fake.calls
    assert db.get("movie:1")["bazarr_sync"] == 1


def test_own_sync_without_job_counts_as_synced(arr, tmp_path):
    movie_with_english(arr)
    arr.subtitle_sync_job = None  # gammel Bazarr: synken er færdig, når kaldet svarer
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), sync_after_upload=True)
    r.run_once()
    assert db.get("movie:1")["bazarr_sync"] == 1


@pytest.mark.parametrize("fail", [500, "job"])
def test_own_sync_failure_is_warned_and_plex_still_called(arr, tmp_path, caplog, fail):
    movie_with_english(arr)
    if fail == "job":
        arr.subtitle_sync_job = "failed"
    else:
        arr.subtitle_sync_status = fail
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic(), sync_after_upload=True)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["bazarr_sync"] == 0 and row["plex_refresh"] == 1
    warning = next(x for x in caplog.records if x.levelname == "WARNING")
    assert warning.message.startswith("ADVARSEL: Bazarr")
    assert "Film 1" in warning.message and "ikke synkroniseret" in warning.message
    assert fake.calls[-1][0] == "/library/sections/1/refresh"


def test_own_sync_timeout_is_warned_and_plex_still_called(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.subtitle_sync_job = "running"  # bliver aldrig færdig
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic(), sync_after_upload=True)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["bazarr_sync"] == 0 and row["plex_refresh"] == 1
    assert "har ikke synkroniseret den danske tekst til Film 1 inden for 10 minutter" in caplog.text
    assert fake.calls[-1][0] == "/library/sections/1/refresh"


def test_hanging_own_sync_call_hits_timeout(arr, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(synk, "SYNC_TIMEOUT", 0.2)
    monkeypatch.setattr(clients, "HARD_MARGIN", 0.2)
    movie_with_english(arr)
    arr.on_subtitle_sync = lambda form: arr.release.wait(30)
    r, db, fake = plex_runner(arr, tmp_path, claude=FakeAnthropic(), sync_after_upload=True)
    try:
        with caplog.at_level(logging.INFO, logger="oversaetter"):
            r.run_once()
    finally:
        arr.release.set()
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["bazarr_sync"] == 0 and row["plex_refresh"] == 1
    assert "ADVARSEL: Bazarr kunne ikke synkronisere den danske tekst til Film 1" in caplog.text


def test_config_switch_default_off():
    assert Config.from_env({}).sync_after_upload is False
    assert Config.from_env({"SYNC_AFTER_UPLOAD": "true"}).sync_after_upload is True
    assert Config.from_env({"SYNC_AFTER_UPLOAD": "false"}).sync_after_upload is False


# --- 4) ♪ ud før oversættelsen, uanset kilde og placering - og aldrig til Bazarr --------------------------------
LYRICS_SDH = srt(
    ("00:00:10,000", "00:00:12,000", "Where is the car?"),
    ("00:00:13,000", "00:00:15,000", "♪ Oh, Brookie, we love you ♪"),  # begge ender
    ("00:00:16,000", "00:00:18,000", "♪ I've been"),  # først
    ("00:00:19,000", "00:00:21,000", "inside my head ♪"),  # sidst
    ("00:00:22,000", "00:00:24,000", "‪♪ Brookie, Brookie ♪‬"),  # Amazons usynlige retningstegn
    ("00:00:25,000", "00:00:27,000", '"♪ Happy birthday ♪"'),  # i citationstegn
    ("00:00:28,000", "00:00:30,000", "She sang ♪ la la ♪ all night."),  # midt i
    ("00:00:31,000", "00:00:33,000", "- ♪ Oh, Brookie ♪\n- Thank you."),
    ("00:00:34,000", "00:00:36,000", "# Lyric line with hash #"),
    ("00:00:37,000", "00:00:39,000", "I have 3 sisters."),
)


def keeps_notes(kwargs):
    """Falsk model, der beholder ♪ i oversættelsen, hvis den engelske replik har dem."""
    lines = [
        {"i": x["i"], "da": f"♪ Dansk {x['i']} ♪" if "♪" in x["en"] else DANISH.get(x["en"], f"Dansk {x['i']}")}
        for x in payload(kwargs)["lines"]
    ]
    return reply(json.dumps({"lines": lines}, ensure_ascii=False))


def assert_no_notes(arr, claude, video):
    sent = [x["en"] for r in claude.requests if "lines" in payload(r) for x in payload(r)["lines"]]
    assert sent and not any("♪" in t or "#" in t for t in sent), sent  # ingen sang sendt til modellen
    assert sent == ["Where is the car?", "I have 3 sisters."]
    content = arr.uploads[0]["content"]
    assert "♪" not in content and "♫" not in content
    assert "Hvor er bilen?" in content and "Jeg har 3 søstre." in content
    assert open(os.path.splitext(video)[0] + ".da.srt", encoding="utf-8").read() == content


def test_embedded_sdh_track_with_lyrics_gives_no_notes(arr, tmp_path):
    video = arr.add_movie(1, 101, {"days": 2}, [{**embedded("en", 11225, track=3), "hi": True}])
    commands = FakeCommands(streams=[stream(3, title="English [SDH]", hi=1)], track_srt=LYRICS_SDH)
    claude = FakeAnthropic(translate=keeps_notes)
    r, db = runner(arr, tmp_path, claude=claude, commands=commands)
    r.run_once()
    assert [c[0] for c in commands.calls] == ["ffprobe", "ffmpeg"]
    assert db.get("movie:1")["status"] == "faerdig"
    assert_no_notes(arr, claude, video)


def test_downloaded_en_hi_srt_with_lyrics_gives_no_notes(arr, tmp_path):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.hi.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(LYRICS_SDH)
    arr.movies[1]["subtitles"] = [en_sub(en_path, hi=True)]
    claude = FakeAnthropic(translate=keeps_notes)
    r, db = runner(arr, tmp_path, claude=claude)
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig"
    assert_no_notes(arr, claude, video)


@pytest.mark.parametrize(
    "text",
    [
        "♪ Oh, Brookie ♪",
        "♪ I've been",
        "inside my head ♪",
        "‪♪ Brookie ♪‬",
        '"♪ Happy birthday ♪"',
        "She sang ♪ la la ♪ all night.",
        "♫ Oh, Brookie ♫",
        "<i>♪ Oh, Brookie ♪</i>",
        "{\\an8}♪ Oh, Brookie ♪",
        "– ♪ Oh, Brookie ♪",
        "♪ I've been\ninside my head ♪",
        "Okay.\n♪ la la",
    ],
)
def test_clean_drops_every_cue_with_notes(text):
    r = hi.clean([cue(0, 1, text), cue(2, 3, "Hello.")], quiet=True)
    assert [c.text for c in r.cues] == ["Hello."]


def test_clean_keeps_numbers_and_hashtags():
    r = hi.clean([cue(0, 1, "#1 fan"), cue(2, 3, "Call me, #2.")], quiet=True)
    assert [c.text for c in r.cues] == ["#1 fan", "Call me, #2."]


def test_notes_added_by_model_are_removed_before_upload(arr, tmp_path, caplog):
    """Sidste sikkerhed: selv hvis modellen skriver ♪ i en almindelig replik, får Bazarr den aldrig."""
    video, _ = movie_with_english(arr)

    def adds_notes(kwargs):
        lines = [
            {"i": x["i"], "da": "♪ Hvor er bilen? ♪" if x["en"] == "Where is the car?" else DANISH[x["en"]]}
            for x in payload(kwargs)["lines"]
        ]
        return reply(json.dumps({"lines": lines}, ensure_ascii=False))

    r, db = runner(arr, tmp_path, claude=FakeAnthropic(translate=adds_notes))
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert db.get("movie:1")["status"] == "faerdig"
    content = arr.uploads[0]["content"]
    assert "♪" not in content and "Hvor er bilen?" not in content and "Jeg har 3 søstre." in content
    warning = next(x for x in caplog.records if x.levelname == "WARNING")
    assert warning.message.startswith("ADVARSEL: 1 danske replikker i Film 1 indeholdt stadig ♪")
    assert "♪ Hvor er bilen? ♪" in warning.message


def test_drop_note_cues():
    kept, removed = hi.drop_note_cues([cue(0, 1, "Hej."), cue(2, 3, "♪ la"), cue(4, 5, "Nej ♫")])
    assert [c.text for c in kept] == ["Hej."] and [c.text for c in removed] == ["♪ la", "Nej ♫"]
