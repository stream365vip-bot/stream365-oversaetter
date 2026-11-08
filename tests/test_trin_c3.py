"""Trin C3 (8.11.2026): nye filer vises først i Plex, når de har dansk tale eller dansk tekst (sikkerhedsnet efter
24 timer). Alt med fakes; intet netværk.
"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from oversaetter import api, udgiv
from oversaetter.db import Database, iso
from oversaetter.udgiv import Revealer
from tests.fakes import FakeArr
from tests.test_direct import FakeAnthropic, FakeCommands, movie_with_english, runner, stream
from tests.test_trin_b4g import TOKEN, FakePlex, cfg_with_plex, with_plex

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def at(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


@pytest.fixture
def plex(arr, tmp_path):
    fake = FakePlex({"1": [str(tmp_path / "Media")]})
    with_plex(arr, fake)
    return fake


def refreshes(fake) -> list[str]:
    return [c[1]["path"] for c in fake.calls if c[0].endswith("/refresh")]


def make(arr, tmp_path, commands=None, started_hours=48, **kw):
    cfg = cfg_with_plex(tmp_path, reveal=True, **kw)
    db = Database(os.path.join(cfg.data_dir, "oversaetter.db"))
    if started_hours is not None:
        db.save_lookup(udgiv.STARTED_KEY, json.dumps(iso(NOW - timedelta(hours=started_hours))))
    clock = {"now": NOW}
    r = Revealer(cfg, db, http=arr.client(), run_cmd=commands or FakeCommands(), now=lambda: clock["now"])
    return r, db, clock


def episode(arr, ep_id, hours_ago=1, series_id=60, season=1, path_in_history=True):
    video = arr.add_episode(ep_id, series_id, 600 + series_id, {"hours": hours_ago}, [], season=season)
    data = {"importedPath": video} if path_in_history else {}
    arr.sonarr_history.append(
        {
            "episodeId": ep_id,
            "seriesId": series_id,
            "date": at(hours=hours_ago),
            "sourceTitle": f"Serie.S01E{ep_id:02d}",
            "data": data,
        }
    )
    return video


def movie(arr, movie_id, hours_ago=1, **extra):
    video = arr.add_movie(movie_id, 900 + movie_id, {"hours": hours_ago}, [], requested=False)
    arr.movies[movie_id].update(extra)
    arr.radarr_history.append(
        {
            "movieId": movie_id,
            "date": at(hours=hours_ago),
            "sourceTitle": f"Film.{movie_id}.2026",
            "data": {"importedPath": video},
        }
    )
    return video


def danish_file(video):
    with open(os.path.splitext(video)[0] + ".da.srt", "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:02,000\nHej\n")


def rows(db):
    return {r["key"]: dict(r) for r in db.conn.execute("SELECT * FROM reveal")}


# --- grundlaget ----------------------------------------------------------------------------------------------
def test_off_by_default(tmp_path):
    assert udgiv.start(cfg_with_plex(tmp_path), None) is None


def test_config_from_env():
    from oversaetter.config import Config

    c = Config.from_env({"REVEAL": "true", "REVEAL_SAFETY_HOURS": "12"})
    assert c.reveal is True and c.reveal_safety_hours == 12.0 and c.reveal_interval_seconds == 90.0


def test_new_episode_without_danish_stays_hidden(arr, tmp_path, plex):
    episode(arr, 5)
    r, db, _ = make(arr, tmp_path)
    assert r.run_once() == 0
    assert rows(db)["episode:5"]["status"] == "venter" and refreshes(plex) == []


def test_danish_subtitle_file_reveals_the_folder(arr, tmp_path, plex):
    video = episode(arr, 5)
    r, db, _ = make(arr, tmp_path)
    r.run_once()
    danish_file(video)  # vores oversættelse (eller Bazarr) har lagt teksten
    assert r.run_once() == 1
    row = rows(db)["episode:5"]
    assert row["status"] == "vist" and row["shown_by"] == "klar" and row["reason"] == "dansk tekstfil"
    assert refreshes(plex) == [os.path.dirname(video)]
    assert all(c[2] == TOKEN for c in plex.calls)
    assert r.run_once() == 0 and len(refreshes(plex)) == 1  # kun én gang


def test_danish_audio_is_shown_at_once_and_probed_once(arr, tmp_path, plex):
    commands = FakeCommands(audio=("dan", "eng"))
    movie(arr, 1)
    episode(arr, 7, season=2)
    r, db, _ = make(arr, tmp_path, commands=commands)
    assert r.run_once() == 2
    assert rows(db)["movie:1"]["reason"] == "dansk tale"
    r.run_once()
    assert len(commands.audio_calls) == 2  # én pr. fil, aldrig igen


def test_embedded_danish_subtitle_counts(arr, tmp_path, plex):
    movie(arr, 1)
    r, db, _ = make(arr, tmp_path, commands=FakeCommands(streams=[stream(2, lang="dan")]))
    assert r.run_once() == 1 and rows(db)["movie:1"]["reason"] == "indlejret dansk tekst"


def test_danish_original_language_needs_no_probe(arr, tmp_path, plex):
    commands = FakeCommands()
    movie(arr, 1, originalLanguage={"id": 6, "name": "Danish"})
    r, db, _ = make(arr, tmp_path, commands=commands)
    assert r.run_once() == 1 and rows(db)["movie:1"]["reason"] == "dansk originalsprog"
    assert commands.audio_calls == []


def test_one_missing_episode_holds_back_the_season_folder(arr, tmp_path, plex):
    a, b = episode(arr, 5), episode(arr, 6)
    other = episode(arr, 9, series_id=61)
    r, db, _ = make(arr, tmp_path)
    danish_file(a)
    danish_file(other)
    assert r.run_once() == 1  # kun den anden serie
    assert rows(db)["episode:5"]["status"] == "venter" and refreshes(plex) == [os.path.dirname(other)]
    danish_file(b)
    assert r.run_once() == 2
    assert refreshes(plex)[-1] == os.path.dirname(a)


def test_safety_net_after_24_hours(arr, tmp_path, plex, caplog):
    caplog.set_level(logging.INFO, logger="oversaetter")
    episode(arr, 5, hours_ago=2)
    r, db, clock = make(arr, tmp_path)
    r.run_once()
    clock["now"] = NOW + timedelta(hours=22, minutes=1)
    assert r.run_once() == 1
    row = rows(db)["episode:5"]
    assert row["shown_by"] == "sikkerhedsnet" and row["reason"] == "intet dansk efter 24 timer"
    assert any("UDEN dansk tekst" in rec.getMessage() for rec in caplog.records)


def test_late_file_does_not_wait_for_a_new_one(arr, tmp_path, plex):
    """Sikkerhedsnettet for det gamle afsnit viser mappen, selv om et nyt afsnit i samme mappe lige er kommet."""
    episode(arr, 5, hours_ago=30)
    episode(arr, 6, hours_ago=1)
    r, db, _ = make(arr, tmp_path)
    assert r.run_once() == 2
    got = rows(db)
    assert got["episode:5"]["shown_by"] == "sikkerhedsnet" and got["episode:6"]["shown_by"] == "sikkerhedsnet"


def test_imports_before_start_are_left_alone(arr, tmp_path, plex):
    episode(arr, 5, hours_ago=3)
    r, db, _ = make(arr, tmp_path, started_hours=None)  # første kørsel: start = nu
    r.run_once()
    assert rows(db) == {} and refreshes(plex) == []
    assert db.lookup(udgiv.STARTED_KEY) is not None


def test_replaced_file_and_new_import(arr, tmp_path, plex):
    video = episode(arr, 5, hours_ago=3)
    r, db, _ = make(arr, tmp_path)
    r.run_once()
    # Sonarr opgraderer: ny import af samme afsnit (samme sti i testen)
    arr.sonarr_history.append(
        {"episodeId": 5, "seriesId": 60, "date": at(hours=1), "sourceTitle": "ny", "data": {"importedPath": video}}
    )
    r.run_once()
    states = sorted((x["import_at"], x["status"]) for x in db.conn.execute("SELECT * FROM reveal"))
    assert [s for _, s in states] == ["erstattet", "venter"]
    os.remove(video)
    r.run_once()
    assert all(x["status"] == "erstattet" for x in db.conn.execute("SELECT * FROM reveal"))


def test_missing_folder_is_not_marked_replaced(arr, tmp_path, plex):
    """Er hele mappen væk (drevet nede), ventes der i stedet for at opgive filen."""
    video = episode(arr, 5)
    r, db, _ = make(arr, tmp_path)
    r.run_once()
    os.rename(os.path.dirname(video), os.path.dirname(video) + "-x")
    r.run_once()
    assert rows(db)["episode:5"]["status"] == "venter"


def test_path_is_looked_up_when_history_has_none(arr, tmp_path, plex):
    video = episode(arr, 5, path_in_history=False)
    r, db, _ = make(arr, tmp_path)
    r.run_once()
    assert rows(db)["episode:5"]["path"] == video


def test_plex_error_keeps_it_waiting(arr, tmp_path, plex):
    video = episode(arr, 5)
    danish_file(video)
    r, db, _ = make(arr, tmp_path)
    plex.fail = 500
    assert r.run_once() == 0 and rows(db)["episode:5"]["status"] == "venter"
    plex.fail = None
    assert r.run_once() == 1


def test_dry_run_does_not_call_plex(arr, tmp_path, plex):
    danish_file(episode(arr, 5))
    r, db, _ = make(arr, tmp_path, dry_run=True)
    assert r.run_once() == 0 and refreshes(plex) == [] and rows(db)["episode:5"]["status"] == "venter"


def test_history_error_does_not_crash(arr, tmp_path, plex, caplog):
    caplog.set_level(logging.INFO, logger="oversaetter")
    arr.fail["sonarr"] = 500
    movie(arr, 1, originalLanguage={"name": "Danish"})
    r, db, _ = make(arr, tmp_path)
    assert r.run_once() == 1
    assert any("kunne ikke hente nye importer fra Sonarr" in rec.getMessage() for rec in caplog.records)


# --- oversætteren -----------------------------------------------------------------------------------------------
def test_translator_leaves_plex_to_the_reveal_step(arr, tmp_path, plex):
    video, _ = movie_with_english(arr)
    token_file = tmp_path / "plex_token.txt"
    token_file.write_text(TOKEN, encoding="utf-8")
    r, db = runner(
        arr, tmp_path, claude=FakeAnthropic(), plex_url="http://plex", plex_token_file=str(token_file), reveal=True
    )

    class Wake:
        def __init__(self):
            self.wake = __import__("threading").Event()

    r.revealer = Wake()
    db.conn.execute(
        "INSERT INTO reveal (key, import_at, path, folder, status) VALUES ('movie:1', ?, ?, ?, 'venter')",
        (iso(NOW), video, os.path.dirname(video)),
    )
    db.conn.commit()
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig"
    assert plex.calls == [] and db.get("movie:1")["plex_refresh"] is None
    assert r.revealer.wake.is_set()


# --- adressen til Fangarr ------------------------------------------------------------------------------------
def test_api_reveal_lists_waiting_without_full_path(arr, tmp_path, plex):
    episode(arr, 5, hours_ago=2)
    danish_file(episode(arr, 9, series_id=61))
    r, db, _ = make(arr, tmp_path)
    r.run_once()
    db.upsert("episode:5", media_type="episode", media_id=5, title="Serie 60", status="soeger")

    class R:
        revealer = r

    out = api.Api(r.cfg, R(), clock=lambda: NOW).reveal({})
    assert out["enabled"] is True and out["safety_hours"] == 24.0 and out["last_run"]
    (w,) = out["waiting"]
    assert w["key"] == "episode:5" and w["file"].endswith("E05.mkv") and "path" not in w
    assert w["age_hours"] == 2.0 and w["translation"] == "soeger"
    assert w["reveal_by"].startswith("2026-11-11T10:00")
    assert [s["key"] for s in out["shown"]] == ["episode:9"] and out["shown"][0]["shown_by"] == "klar"
    assert str(tmp_path) not in json.dumps(out)


def test_api_reveal_without_table(tmp_path):
    from tests.helpers import cfg as make_cfg

    out = api.Api(make_cfg(data_dir=str(tmp_path / "tom"))).reveal({})
    assert out["waiting"] == [] and out["enabled"] is False


def test_old_title_translated_later_still_gets_plex_scan(arr, tmp_path, plex):
    """En ældre titel (fx mærket oversaet), der ikke venter på at blive vist, får Plex-scanningen som før."""
    video, _ = movie_with_english(arr)
    token_file = tmp_path / "plex_token.txt"
    token_file.write_text(TOKEN, encoding="utf-8")
    r, db = runner(
        arr, tmp_path, claude=FakeAnthropic(), plex_url="http://plex", plex_token_file=str(token_file), reveal=True
    )

    class Wake:
        def __init__(self):
            self.wake = __import__("threading").Event()

    r.revealer = Wake()
    r.run_once()
    assert refreshes(plex) == [os.path.dirname(video)] and not r.revealer.wake.is_set()
