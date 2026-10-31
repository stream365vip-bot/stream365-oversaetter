"""Trin B9 (31.10.2026): titler med dansk lyd oversættes aldrig, og titler på en dansk streamingtjeneste venter på
en officiel dansk tekst (PROVIDER_WAIT_DAYS_MOVIES/SERIES), før vi oversætter. Alt med fakes (Bazarr, Radarr, Sonarr,
Seerr, TMDB, ffprobe og Anthropic); ingen netværk.
"""

import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from oversaetter.__main__ import forget
from oversaetter.config import Config
from oversaetter.db import Database
from oversaetter.tjenester import arr_language, dk_providers
from tests.fakes import FakeArr, en_sub
from tests.test_direct import EN, FakeAnthropic, FakeCommands, movie_with_english, runner
from tests.test_trin_b7 import FakeFeed, feed_runner

TMDB = {"tmdb_url": "http://tmdb/3", "tmdb_api_key": "tmdb-key"}


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def b9_runner(arr, tmp_path, claude=None, **kw):
    return runner(arr, tmp_path, claude=claude or FakeAnthropic(), **{**TMDB, **kw})


def episode_with_english(arr, ep_id, series_id=50, tvdb_id=500):
    video = arr.add_episode(ep_id, series_id, tvdb_id, {"days": 2}, [], tags=[7])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[ep_id]["subtitles"] = [en_sub(en_path)]
    return video


def days_pass(db, days: float) -> None:
    """Flyt alle gemte tidspunkter (ventetid, søgninger, opslag) så mange dage tilbage."""
    shift = timedelta(days=days)
    for col in ("wait_started_at", "search_at", "providers_checked_at", "updated_at"):
        for key, value in db.conn.execute(f"SELECT key, {col} FROM items").fetchall():
            if value:
                when = datetime.fromisoformat(value) - shift
                db.conn.execute(f"UPDATE items SET {col} = ? WHERE key = ?", (when.isoformat(), key))
    for key, value in db.conn.execute("SELECT key, checked_at FROM lookups").fetchall():
        db.conn.execute(
            "UPDATE lookups SET checked_at = ? WHERE key = ?",
            ((datetime.fromisoformat(value) - shift).isoformat(), key),
        )
    db.conn.commit()


def messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def provider_calls(arr) -> list[str]:
    return [p for p in arr.tmdb_calls if p.endswith("/watch/providers")]


def round_line(caplog) -> str:
    return [m for m in messages(caplog) if m.startswith("Runde:")][-1]


# --- rene funktioner -----------------------------------------------------------------------------------------
def test_dk_providers_uses_flatrate_free_and_ads_only():
    data = {
        "results": {
            "DK": {
                "flatrate": [{"provider_name": "Netflix"}, {"provider_name": "Viaplay"}],
                "free": [{"provider_name": "DR TV"}],
                "ads": [{"provider_name": "TV 2 Play"}, {"provider_name": "Netflix"}],
                "rent": [{"provider_name": "Apple TV"}],
                "buy": [{"provider_name": "Blockbuster"}],
            },
            "SE": {"flatrate": [{"provider_name": "SVT Play"}]},
        }
    }
    assert dk_providers(data) == ["Netflix", "Viaplay", "DR TV", "TV 2 Play"]
    assert dk_providers({"results": {"SE": {"flatrate": [{"provider_name": "SVT Play"}]}}}) == []
    assert dk_providers({}) == []


def test_arr_language():
    assert arr_language({"originalLanguage": {"id": 7, "name": "Danish"}}) == "da"
    assert arr_language({"originalLanguage": {"id": 1, "name": "English"}}) == "english"
    assert arr_language({"originalLanguage": {"id": 0, "name": "Unknown"}}) is None
    assert arr_language({}) is None


def test_config_defaults_and_env():
    default = Config.from_env({})
    assert (default.provider_wait, default.provider_wait_days_movies, default.provider_wait_days_series) == (
        True,
        7.0,
        3.0,
    )
    cfg = Config.from_env(
        {"PROVIDER_WAIT": "false", "PROVIDER_WAIT_DAYS_MOVIES": "10", "PROVIDER_WAIT_DAYS_SERIES": "1,5"}
    )
    assert (cfg.provider_wait, cfg.provider_wait_days_movies, cfg.provider_wait_days_series) == (False, 10.0, 1.5)


def test_old_database_gets_new_fields_and_lookups(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (key TEXT PRIMARY KEY, media_type TEXT NOT NULL, media_id INTEGER NOT NULL, "
        "title TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO items (key, media_type, media_id, title, status) VALUES ('movie:1', 'movie', 1, 'A', 'soegt')"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    cols = {row[1] for row in db.conn.execute("PRAGMA table_info(items)")}
    assert {"dk_providers", "providers_checked_at", "wait_started_at"} <= cols
    assert db.get("movie:1")["title"] == "A"
    db.save_lookup("providers:movie:1", '{"names": []}')
    assert db.lookup("providers:movie:1")[0] == '{"names": []}'


# --- ventereglen: film -------------------------------------------------------------------------------------
def test_netflix_movie_waits_then_danish_found_day_2_nothing_translated(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    arr.tmdb["movie:101"] = {"original_language": "en", "DK": {"flatrate": ["Netflix"]}}
    arr.movies[1]["originalLanguage"] = {"name": "English"}
    claude = FakeAnthropic()
    fake = FakeFeed()
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=claude, **TMDB)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
        assert db.get("movie:1")["status"] == "venter_paa_dansk"
        assert "Venter på dansk: Film 1 ligger på Netflix (højst 7 dage)" in messages(caplog)
        assert ", 1 venter på dansk, " in round_line(caplog)
        # Samme dag: ingen ny søgning, intet nyt opslag og ingen ny "Venter på dansk"-linje
        caplog.clear()
        r.run_once()
        assert len(arr.search_calls) == 1 and len(provider_calls(arr)) == 1
        assert not any(m.startswith("Venter på dansk") for m in messages(caplog))
        assert ", 1 venter på dansk, " in round_line(caplog)
        # Dag 2: den daglige søgning finder dansk
        days_pass(db, 1)
        arr.search_finds_danish.add("movie:1")
        caplog.clear()
        r.run_once()
    assert len(arr.search_calls) == 2
    assert any("ventede på dansk; ligger på Netflix" in m for m in messages(caplog))
    assert any(
        "Søger nu efter dansk til Film 1 (Seerr, venter på dansk - daglig søgning)" in m for m in messages(caplog)
    )
    assert db.get("movie:1")["status"] == "faerdig"
    assert claude.requests == [] and arr.uploads == []
    assert fake.posts == []  # ikke en oversættelse - intet til feedet
    assert "Hej" in open(os.path.splitext(video)[0] + ".da.srt", encoding="utf-8").read()  # Bazarrs egen tekst


def test_viaplay_movie_without_danish_is_translated_on_day_8(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.tmdb["movie:101"] = {"original_language": "en", "DK": {"flatrate": ["Viaplay"]}}
    claude = FakeAnthropic()
    r, db = b9_runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
        for _ in range(6):  # dag 2-7: én søgning og ét opslag om dagen, stadig ventetid
            days_pass(db, 1)
            r.run_once()
            assert db.get("movie:1")["status"] == "venter_paa_dansk"
        assert len(arr.search_calls) == 7 and len(provider_calls(arr)) == 7
        assert claude.requests == []
        days_pass(db, 1)  # dag 8
        r.run_once()
    assert any(m.startswith("Ventetiden udløb: Film 1 har ventet 7 dage") for m in messages(caplog))
    assert [m for m in messages(caplog) if m.startswith("Venter på dansk")] == [
        "Venter på dansk: Film 1 ligger på Viaplay (højst 7 dage)"
    ]
    assert db.get("movie:1")["status"] == "faerdig" and len(arr.uploads) == 1
    assert claude.kinds()[0] == "oversæt"


def test_movie_without_dk_provider_is_translated_at_once(arr, tmp_path):
    movie_with_english(arr)
    arr.tmdb["movie:101"] = {"original_language": "en", "DK": {"rent": ["Apple TV"], "buy": ["SF Anytime"]}}
    r, db = b9_runner(arr, tmp_path)
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig" and len(arr.uploads) == 1
    assert db.get("movie:1")["dk_providers"] == "[]"


def test_tmdb_down_translates_at_once_with_one_log_line(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.fail["tmdb"] = 503
    r, db = b9_runner(arr, tmp_path)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    warnings = [m for m in messages(caplog) if "danske streamingtjenester op" in m]
    assert len(warnings) == 1 and "Film 1" in warnings[0] and "tmdb-key" not in warnings[0]
    assert db.get("movie:1")["status"] == "faerdig" and len(arr.uploads) == 1


def test_tmdb_unknown_title_and_no_key_translate_at_once(arr, tmp_path, caplog):
    movie_with_english(arr)  # TMDB kender ikke 101 (HTTP 404)
    r, db = b9_runner(arr, tmp_path)
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig"
    arr2 = FakeArr(str(tmp_path / "Media2"))
    movie_with_english(arr2)
    r2, db2 = runner(arr2, tmp_path / "b", claude=FakeAnthropic(), tmdb_api_key_file=str(tmp_path / "ingen.txt"))
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r2.log_start()
        r2.run_once()
    assert arr2.tmdb_calls == [] and db2.get("movie:1")["status"] == "faerdig"
    assert any(m.startswith("Ingen TMDB-nøgle") for m in messages(caplog))


def test_provider_wait_false_works_as_before(arr, tmp_path):
    movie_with_english(arr)
    arr.tmdb["movie:101"] = {"original_language": "en", "DK": {"flatrate": ["Netflix"]}}
    r, db = b9_runner(arr, tmp_path, provider_wait=False)
    r.run_once()
    assert provider_calls(arr) == []
    assert db.get("movie:1")["status"] == "faerdig" and len(arr.uploads) == 1


def test_forget_resets_the_waiting_time(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.tmdb["movie:101"] = {"original_language": "en", "DK": {"flatrate": ["Netflix"]}}
    r, db = b9_runner(arr, tmp_path)
    r.run_once()
    days_pass(db, 5)
    assert forget(r.cfg, "movie:1", db=db) == 1  # venter_paa_dansk kræver --alle (trin B8)
    assert forget(r.cfg, "movie:1", db=db, all_statuses=True) == 0
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
        days_pass(db, 3)  # 5 + 3 dage før glem ville være over 7; efter glem kun 3
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "venter_paa_dansk" and arr.uploads == []
    assert "Venter på dansk: Film 1 ligger på Netflix (højst 7 dage)" in messages(caplog)


def test_danish_found_by_bazarr_itself_while_waiting(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    arr.tmdb["movie:101"] = {"original_language": "en", "DK": {"flatrate": ["Netflix"]}}
    r, db = b9_runner(arr, tmp_path)
    r.run_once()
    da = os.path.splitext(video)[0] + ".da.srt"
    with open(da, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:02,000\nHej\n")
    arr.movies[1]["subtitles"].append({"code2": "da", "code3": "dan", "path": da, "forced": False})
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()  # filmen er væk fra Bazarrs wanted-liste
    assert db.get("movie:1")["status"] == "sprunget_over"
    assert any("har allerede dansk (ventede på dansk; ligger på Netflix)" in m for m in messages(caplog))
    assert arr.uploads == []


# --- ventereglen: serier -----------------------------------------------------------------------------------
def test_episode_on_tv2_play_is_translated_on_day_4(arr, tmp_path, caplog):
    episode_with_english(arr, 5)
    arr.series[50]["tmdbId"] = 900
    arr.tmdb["tv:900"] = {"original_language": "en", "DK": {"ads": ["TV 2 Play"]}}
    claude = FakeAnthropic()
    r, db = b9_runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
        for _ in range(2):  # dag 2-3
            days_pass(db, 1)
            r.run_once()
        assert db.get("episode:5")["status"] == "venter_paa_dansk" and claude.requests == []
        days_pass(db, 1)  # dag 4
        r.run_once()
    assert "Venter på dansk: Serie 50 1x05 ligger på TV 2 Play (højst 3 dage)" in messages(caplog)
    assert any(m.startswith("Ventetiden udløb: Serie 50 1x05 har ventet 3 dage") for m in messages(caplog))
    assert db.get("episode:5")["status"] == "faerdig" and len(arr.uploads) == 1


def test_series_is_looked_up_once_and_shared_by_its_episodes(arr, tmp_path):
    for ep in (5, 6, 7):
        episode_with_english(arr, ep)
    arr.tmdb_tvdb[500] = 900  # Sonarr har intet tmdbId - findes via tvdbId
    arr.tmdb["tv:900"] = {"original_language": "en", "DK": {"flatrate": ["HBO Max"]}}
    r, db = b9_runner(arr, tmp_path)
    r.run_once()
    r.run_once()
    assert [db.get(f"episode:{ep}")["status"] for ep in (5, 6, 7)] == ["venter_paa_dansk"] * 3
    assert provider_calls(arr) == ["/3/tv/900/watch/providers"]
    assert [p for p in arr.tmdb_calls if p.startswith("/3/find/")] == ["/3/find/500"]
    assert db.get("episode:6")["dk_providers"] == '["HBO Max"]'


# --- dansk lyd -----------------------------------------------------------------------------------------------
def test_danish_original_language_from_tmdb_is_skipped_without_anything(arr, tmp_path, caplog):
    movie_with_english(arr)  # Radarr har intet originalsprog - TMDB bruges
    arr.tmdb["movie:101"] = {"original_language": "da", "DK": {"flatrate": ["Netflix"]}}
    claude = FakeAnthropic()
    fake = FakeFeed()
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=claude, **TMDB)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "ikke_muligt" and "dansk lyd, ingen tekst nødvendig" in row["reason"]
    assert "Springer over: Film 1 - dansk lyd, ingen tekst nødvendig (originalsprog dansk ifølge TMDB)" in messages(
        caplog
    )
    assert arr.search_calls == [] and provider_calls(arr) == [] and claude.requests == [] and fake.posts == []
    # Næste runde: allerede behandlet, intet nyt
    r.run_once()
    assert arr.tmdb_calls == ["/3/movie/101"]


def test_radarr_language_is_used_first_without_tmdb(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.movies[1]["originalLanguage"] = {"id": 7, "name": "Danish"}
    commands = FakeCommands()
    r, db = b9_runner(arr, tmp_path, commands=commands)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert arr.tmdb_calls == [] and commands.audio_calls == []
    assert "Springer over: Film 1 - dansk lyd, ingen tekst nødvendig (originalsprog dansk ifølge Radarr)" in messages(
        caplog
    )


def test_english_movie_with_danish_dub_is_skipped(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.movies[1]["originalLanguage"] = {"name": "English"}
    commands = FakeCommands(audio=("eng", "dan"))
    claude = FakeAnthropic()
    r, db = b9_runner(arr, tmp_path, claude=claude, commands=commands)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
        r.run_once()
    assert db.get("movie:1")["status"] == "ikke_muligt"
    assert "Springer over: Film 1 - dansk lyd, ingen tekst nødvendig (dansk lydspor i filen)" in messages(caplog)
    assert arr.search_calls == [] and claude.requests == [] and arr.tmdb_calls == []
    assert len(commands.audio_calls) == 1


def test_english_movie_without_danish_audio_as_before(arr, tmp_path):
    movie_with_english(arr)
    arr.movies[1]["originalLanguage"] = {"name": "English"}
    commands = FakeCommands(audio=("eng", "ger"))
    r, db = b9_runner(arr, tmp_path, commands=commands)
    r.run_once()
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig" and len(arr.uploads) == 1
    assert len(commands.audio_calls) == 1  # lydsporene undersøges kun én gang pr. fil


def test_tagged_danish_series_is_never_translated(arr, tmp_path, caplog):
    for ep in (5, 6):
        episode_with_english(arr, ep)
    arr.series[50]["originalLanguage"] = {"name": "Danish"}
    claude = FakeAnthropic()
    r, db = b9_runner(arr, tmp_path, claude=claude)
    r.run_once()
    assert [db.get(f"episode:{ep}")["status"] for ep in (5, 6)] == ["ikke_muligt"] * 2
    assert arr.search_calls == [] and claude.requests == [] and arr.tmdb_calls == []
    assert "originalsprog dansk ifølge Sonarr" in db.get("episode:5")["reason"]


def test_danish_audio_check_applies_also_without_provider_wait(arr, tmp_path):
    movie_with_english(arr)
    arr.movies[1]["originalLanguage"] = {"name": "Danish"}
    r, db = b9_runner(arr, tmp_path, provider_wait=False)
    r.run_once()
    assert db.get("movie:1")["status"] == "ikke_muligt" and arr.uploads == []


def test_waiting_status_survives_restart_and_wait_start_is_kept(arr, tmp_path):
    movie_with_english(arr)
    arr.tmdb["movie:101"] = {"original_language": "en", "DK": {"flatrate": ["Netflix"]}}
    r, db = b9_runner(arr, tmp_path)
    r.run_once()
    first = db.get("movie:1")["wait_started_at"]
    r2, _ = b9_runner(arr, tmp_path)  # samme database, ny proces
    r2.resume()
    r2.run_once()
    row = db.get("movie:1")
    assert row["status"] == "venter_paa_dansk" and row["wait_started_at"] == first
    assert datetime.now(UTC) - datetime.fromisoformat(first) < timedelta(minutes=5)


def test_dry_run_logs_the_wait_but_saves_nothing(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.tmdb["movie:101"] = {"original_language": "en", "DK": {"flatrate": ["Netflix"]}}
    r, db = b9_runner(arr, tmp_path, dry_run=True)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert db.conn.execute("SELECT COUNT(*) FROM lookups").fetchone()[0] == 0
    assert db.get("movie:1") is None and arr.search_calls == [] and arr.uploads == []
