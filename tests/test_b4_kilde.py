"""Opfølgning på trin B4 (18.10.2026): HI/SDH-kilden foretrækkes, logges og gemmes (items.en_hi)."""

import logging
import os
import sqlite3

from oversaetter import hi
from oversaetter.db import Database
from oversaetter.selection import choose_source
from tests.fakes import FakeArr, embedded, en_sub
from tests.helpers import cue, srt
from tests.test_direct import EN, FakeAnthropic, FakeCommands, runner, stream
from tests.test_trin_b4 import run_test


def kinds(caplog):
    return [r.message for r in caplog.records if r.message.startswith("Engelsk kilde:")]


def speakers(n):
    """n replikker med talernavn og et par uden."""
    return [cue(i, i + 0.9, f"RICK: Line {i}.") for i in range(n)] + [cue(10, 10.9, "Hello."), cue(11, 11.9, "Bye.")]


# --- indlejrede spor (falske ffprobe-svar) -------------------------------------------------------------
def test_sdh_track_is_chosen_logged_and_saved(tmp_path, caplog):
    arr = FakeArr(str(tmp_path / "Media"))
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    commands = FakeCommands(streams=[stream(3, title="English"), stream(4, title="English (SDH)")])
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), commands=commands)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    ff = commands.calls[1]
    assert ff[ff.index("-map") + 1] == "0:4"
    assert kinds(caplog) == ["Engelsk kilde: HI (English (SDH))"]
    assert db.get("movie:1")["status"] == "faerdig" and db.get("movie:1")["en_hi"] == 1


def test_single_ordinary_track_is_chosen(tmp_path, caplog):
    arr = FakeArr(str(tmp_path / "Media"))
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    commands = FakeCommands(streams=[stream(2, lang="dan"), stream(3, title="English")])
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), commands=commands)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    ff = commands.calls[1]
    assert ff[ff.index("-map") + 1] == "0:3"
    assert kinds(caplog) == ["Engelsk kilde: almindelig"]
    assert db.get("movie:1")["en_hi"] == 0


def test_hi_disposition_and_text_before_image():
    from oversaetter.direct import pick_stream

    picked, _ = pick_stream([stream(3), stream(4, codec="hdmv_pgs_subtitle", hi=1), stream(5, hi=1)])
    assert picked["index"] == 5  # HI-disposition, og tekst går forud for billedbaseret HI
    picked, _ = pick_stream([stream(3), stream(4, codec="hdmv_pgs_subtitle", title="SDH")])
    assert picked["index"] == 3
    picked, _ = pick_stream([stream(3, title="English"), stream(4, lang="ger", title="SDH")])
    assert picked["index"] == 3  # sproget skal stadig være engelsk


# --- eksterne filer ------------------------------------------------------------------------------------
def test_external_hi_file_is_preferred(tmp_path, caplog):
    arr = FakeArr(str(tmp_path / "Media"))
    video = arr.add_movie(1, 101, {"days": 2}, [])
    stem = os.path.splitext(video)[0]
    for ext in (".en.srt", ".en.hi.srt"):
        with open(stem + ext, "w", encoding="utf-8") as f:
            f.write(EN)
    # Bazarr har ikke sat hi-flaget: filnavnet afgør det.
    arr.movies[1]["subtitles"] = [en_sub(stem + ".en.srt", 1), en_sub(stem + ".en.hi.srt", 2)]
    path, _, desc = choose_source(arr.movies[1]["subtitles"])
    assert path == stem + ".en.hi.srt" and "(HI)" in desc
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    name = os.path.basename(stem + ".en.hi.srt")
    assert kinds(caplog) == [f"Engelsk kilde: HI ({name})"]
    assert db.get("movie:1")["source"] == stem + ".en.hi.srt" and db.get("movie:1")["en_hi"] == 1


def test_sdh_filename():
    assert hi.hi_filename("/x/Film.2020.en.sdh.srt") and hi.hi_filename("Film.en.hi.srt")
    assert not hi.hi_filename("Hi.Score.2020.en.srt") and not hi.hi_filename("Film.en.srt")


# --- indholdsreglen ------------------------------------------------------------------------------------
def test_content_rule_4_and_5_speakers():
    four, five = hi.clean(speakers(4)).stats, hi.clean(speakers(5)).stats
    assert hi.source_kind(four, False, "Film.en.srt") == (False, "Engelsk kilde: almindelig")
    assert hi.source_kind(five, False, "Film.en.srt") == (
        True,
        "Engelsk kilde: HI (Film.en.srt; indhold: 5 lydbeskrivelser/talernavne)",
    )
    assert hi.source_kind(four, True, "English SDH")[0]  # navnet alene er nok


def test_content_rule_in_drift(tmp_path, caplog):
    arr = FakeArr(str(tmp_path / "Media"))
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    text = srt(*((f"00:00:0{i},000", f"00:00:0{i},900", f"RICK: We go now {i}.") for i in range(5)))
    commands = FakeCommands(streams=[stream(3, title="English")], track_srt=text)
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), commands=commands)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert kinds(caplog) == ["Engelsk kilde: HI (English; indhold: 5 lydbeskrivelser/talernavne)"]
    assert db.get("movie:1")["en_hi"] == 1


# --- databasen og oversaet-test ------------------------------------------------------------------------
def test_old_database_gets_en_hi_column(tmp_path):
    path = str(tmp_path / "gammel.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (key TEXT PRIMARY KEY, media_type TEXT NOT NULL, media_id INTEGER NOT NULL, "
        "status TEXT NOT NULL, updated_at TEXT)"
    )
    conn.execute("INSERT INTO items VALUES ('movie:1', 'movie', 1, 'faerdig', NULL)")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.get("movie:1")["en_hi"] is None
    db.upsert("movie:1", en_hi=1)
    assert db.conn.execute("SELECT COUNT(*) FROM items WHERE en_hi = 1").fetchone()[0] == 1


def test_oversaet_test_report_shows_source_kind(tmp_path, capsys):
    for hi_on in (True, False):
        (tmp_path / str(hi_on)).mkdir()
        rc, _, _ = run_test(tmp_path / str(hi_on), hi_on)
        assert rc == 0
        report = capsys.readouterr().out
        assert "Engelsk kilde: HI (film.en.srt; indhold: 8 lydbeskrivelser/talernavne)" in report
        assert "Engelsk fil: " in report
