"""Reglerne for hvornår der oversættes: gyldig søgning først, tjenester på pause, mærker og synkronisering."""

import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from oversaetter.db import Database, iso, now
from oversaetter.runner import Runner
from oversaetter.search import Paused
from oversaetter.translate import request_translation
from tests.fakes import FakeArr, en_sub
from tests.helpers import FakeClaude, response
from tests.test_runner import EN, Clock, movie_with_english, write_haiku
from tests.test_selection import make_cfg


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def runner(arr, tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    db = Database(os.path.join(cfg.data_dir, "oversaetter.db"))
    clock = Clock()
    r = Runner(
        cfg,
        db,
        http=arr.client(),
        claude=FakeClaude(lambda req: response([])),
        translate=lambda b, c, m: request_translation(b, c, m, sleep=clock.sleep, clock=clock),
        sleep=clock.sleep,
        clock=clock,
    )
    return r, db, clock


def run(r, caplog):
    caplog.clear()
    with caplog.at_level("DEBUG", logger="oversaetter"):
        cands = r.run_once()
    return cands, caplog.text


def pause(arr, name="opensubtitlescom", status="TooManyRequests", retry="in 2 hours"):
    for p in arr.providers:
        if p["name"] == name:
            p.update(status=status, retry=retry)


def unpause(arr):
    for p in arr.providers:
        p.update(status="Good", retry="-")


def episode_with_english(arr, tags=(7,)):
    video = arr.add_episode(5, 50, 5000, {"minutes": 1}, [], tags=tags)
    en = os.path.splitext(video)[0] + ".en.srt"
    with open(en, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[5]["subtitles"] = [{"code2": "en", "path": en, "forced": False, "hi": False, "id": 1}]
    return video


# --- Seerr-film --------------------------------------------------------------------------
def test_seerr_movie_without_search_searches_first(arr, tmp_path, caplog):
    movie_with_english(arr, days=0)
    arr.search_job_status = "running"  # søgningen bliver ikke færdig i denne runde
    r, db, clock = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert [c["radarrid"] for c in arr.search_calls] == ["1"] and arr.translate_calls == []
    assert "Søger nu efter dansk til Film 1 (Seerr, ingen gyldig søgning endnu)" in text
    row = db.get("movie:1")
    assert row["status"] == "soeger" and row["search_started_at"] and row["search_at"] is None


def test_search_not_finished_within_limit_waits_then_counts(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    arr.search_job_status = "running"
    r, db, clock = runner(arr, tmp_path, search_timeout_minutes=10)
    _, text = run(r, caplog)
    assert 600 <= clock.t < 700  # ventede højst grænsen på 10 minutter
    assert "Søgningen for Film 1 er ikke færdig efter 10 minutter" in text
    assert "Film 1 - venter på at søgning bliver færdig" in text
    assert arr.translate_calls == [] and db.get("movie:1")["search_at"] is None
    # næste runde: stadig ikke færdig -> ingen ny søgning, ingen oversættelse
    _, text = run(r, caplog)
    assert len(arr.search_calls) == 1 and arr.translate_calls == []
    assert "Film 1 - venter på at søgning bliver færdig" in text
    # Bazarr bliver færdig -> søgningen tæller, og der oversættes
    for job in arr.jobs:
        job["status"] = "completed"
    _, text = run(r, caplog)
    assert len(arr.search_calls) == 1 and len(arr.translate_calls) == 1
    assert db.get("movie:1")["status"] == "faerdig" and db.get("movie:1")["search_at"]


def test_valid_search_without_danish_translates(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert len(arr.search_calls) == 1 and len(arr.translate_calls) == 1
    assert "Film 1 - gyldig søgning fandt ikke dansk - oversætter (Seerr, ekstern engelsk: Film 1.en.srt)" in text
    assert "Gyldig søgning fandt ikke dansk - beder Bazarr oversætte Film 1" in text
    assert db.get("movie:1")["status"] == "faerdig"


def test_valid_search_with_danish_does_nothing(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.search_finds_danish.add("movie:1")
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert len(arr.search_calls) == 1 and arr.translate_calls == []
    assert "Film 1 - gyldig søgning fandt dansk - intet at gøre" in text
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["reason"] == "gyldig søgning fandt dansk"
    run(r, caplog)
    assert len(arr.search_calls) == 1 and arr.translate_calls == []


def test_old_bazarr_without_job_queue_searches_synchronously(arr, tmp_path):
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    arr.jobs = None
    arr.search_job_status = None
    r, db, _ = runner(arr, tmp_path)
    r.run_once()
    assert len(arr.search_calls) == 1 and len(arr.translate_calls) == 1


# --- tjenester på pause ---------------------------------------------------------------------
def test_provider_paused_before_search_waits(arr, tmp_path, caplog):
    movie_with_english(arr)
    pause(arr)
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert arr.search_calls == [] and arr.translate_calls == []
    assert "Film 1 - venter: OpenSubtitles er sat på pause til ca. " in text
    assert db.get("movie:1") is None
    # tjenesten er tilbage -> der søges (og oversættes)
    unpause(arr)
    run(r, caplog)
    assert len(arr.search_calls) == 1


def test_provider_paused_after_search_makes_search_invalid(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    arr.on_search = lambda params: pause(arr, "podnapisi", "ServiceUnavailable", "in 25 minutes")
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert len(arr.search_calls) == 1 and arr.translate_calls == []
    assert "Søgningen for Film 1 tæller ikke: venter: Podnapisi er sat på pause til ca. kl." in text
    row = db.get("movie:1")
    assert row["search_at"] is None and row["search_started_at"] is None
    # stadig på pause: ingen ny søgning
    arr.on_search = None
    run(r, caplog)
    assert len(arr.search_calls) == 1 and arr.translate_calls == []
    # tilbage: ny søgning, som nu er gyldig -> oversættes
    unpause(arr)
    run(r, caplog)
    assert len(arr.search_calls) == 2 and len(arr.translate_calls) == 1


def test_provider_paused_over_24_hours_warns_and_does_not_translate(arr, tmp_path, caplog):
    movie_with_english(arr)
    pause(arr)
    r, db, _ = runner(arr, tmp_path)
    db.track_paused(["opensubtitlescom"], now() - timedelta(hours=25))
    _, text = run(r, caplog)
    warning = "OpenSubtitles har været sat på pause i over 24 timer - overvej at slå den fra i Bazarr"
    assert text.count(warning) == 1
    assert arr.search_calls == [] and arr.translate_calls == []
    _, text = run(r, caplog)
    assert text.count(warning) == 1  # én gang pr. runde
    # når den er tilbage, glemmes tidspunktet
    unpause(arr)
    run(r, caplog)
    assert db.track_paused([]) == {}


def test_provider_status_unreadable_means_no_search(arr, tmp_path, caplog):
    movie_with_english(arr)
    r, db, _ = runner(arr, tmp_path)
    orig = arr.bazarr

    def broken(req, path, q):
        if path == "/api/providers":
            import httpx2

            return httpx2.Response(500)
        return orig(req, path, q)

    arr.bazarr = broken
    _, text = run(r, caplog)
    assert arr.search_calls == [] and arr.translate_calls == []
    assert "Kunne ikke læse Bazarrs tjenestestatus" in text


def test_already_translated_with_valid_search_is_not_blocked_by_pause(arr, tmp_path):
    """En gyldig søgning fra tidligere tæller stadig, selvom en tjeneste er på pause nu."""
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    r, db, _ = runner(arr, tmp_path)
    db.upsert("movie:1", media_type="movie", media_id=1, status="soegt", search_at=iso())
    pause(arr)
    r.run_once()
    assert arr.search_calls == [] and len(arr.translate_calls) == 1


def test_paused_until_text():
    at = datetime(2026, 10, 17, 12, 30).astimezone()
    assert Paused("opensubtitlescom", "TooManyRequests", "in 2 hours").describe(at) == (
        "OpenSubtitles er sat på pause til ca. kl. 14:30"
    )
    assert Paused("podnapisi", "x", "in 25 minutes").until(at) == "til ca. kl. 12:55"
    assert Paused("podnapisi", "x", "in an hour").until(at) == "til ca. kl. 13:30"
    assert Paused("podnapisi", "x", "tomorrow").until(at) == "til ca. 18.10. kl. 12:30"
    assert Paused("podnapisi", "x", "-").until(at) == ""
    assert Paused("subdl", "x", "later").describe(at) == "SubDL er sat på pause (Bazarr: later)"


# --- søgning efter engelsk (beslutning 17.10.2026) ----------------------------------------------
def movie_without_english(arr):
    return arr.add_movie(1, 101, {"days": 2}, [])


def add_english(arr, video):
    en = os.path.splitext(video)[0] + ".en.srt"
    with open(en, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.movies[1]["subtitles"] = [*arr.movies[1]["subtitles"], en_sub(en)]


def languages(arr):
    return [c["language"] for c in arr.search_calls]


def test_danish_found_means_no_english_search(arr, tmp_path, caplog):
    movie_without_english(arr)
    arr.search_finds_danish.add("movie:1")
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert languages(arr) == ["da"] and arr.translate_calls == []
    assert "Søger nu efter engelsk" not in text and db.get("movie:1")["status"] == "faerdig"


def test_no_danish_but_english_exists_translates_without_english_search(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert languages(arr) == ["da"] and len(arr.translate_calls) == 1
    assert "Søger nu efter engelsk" not in text and "Engelsk fundet" not in text
    assert db.get("movie:1")["status"] == "faerdig"


def test_english_search_finds_english_and_translates_same_round(arr, tmp_path, caplog):
    video = movie_without_english(arr)
    arr.on_search = lambda params: add_english(arr, video) if params["language"] == "en" else None
    arr.on_translate = write_haiku(video)
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert languages(arr) == ["da", "en"] and len(arr.translate_calls) == 1
    assert arr.search_calls[1]["hi"] == "False" and arr.search_calls[1]["radarrid"] == "1"
    assert "Søger nu efter engelsk til Film 1 (ingen dansk og ingen engelsk kilde)" in text
    assert "Engelsk fundet til Film 1 - oversætter" in text
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["search_at"] and row["en_search_at"]


def test_english_search_finds_nothing_skips_and_waits_24_hours(arr, tmp_path, caplog):
    movie_without_english(arr)
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert languages(arr) == ["da", "en"] and arr.translate_calls == []
    assert "Springer over: Film 1 - ingen engelsk tekstkilde (søgning efter engelsk fandt intet" in text
    row = db.get("movie:1")
    assert row["status"] == "mangler_engelsk" and row["en_search_at"]
    # næste runde: ingen ny søgning, ingen ny INFO-linje
    _, text = run(r, caplog)
    assert languages(arr) == ["da", "en"] and arr.translate_calls == []
    assert "INFO" not in [rec.levelname for rec in caplog.records if "Springer over: Film 1" in rec.message]
    # over 24 timer efter den forrige søgning: ny søgning efter engelsk (ikke dansk)
    db.upsert("movie:1", en_search_at=iso(now() - timedelta(hours=25)))
    run(r, caplog)
    assert languages(arr) == ["da", "en", "en"]


def test_english_appears_later_is_translated_without_new_search(arr, tmp_path, caplog):
    video = movie_without_english(arr)
    r, db, _ = runner(arr, tmp_path)
    run(r, caplog)
    assert languages(arr) == ["da", "en"] and arr.translate_calls == []
    # Bazarr eller Jesper har hentet en engelsk tekst
    add_english(arr, video)
    arr.on_translate = write_haiku(video)
    _, text = run(r, caplog)
    assert languages(arr) == ["da", "en"] and len(arr.translate_calls) == 1
    assert "Engelsk fundet til Film 1 - oversætter" in text
    assert db.get("movie:1")["status"] == "faerdig"


def test_provider_paused_during_english_search_makes_it_invalid(arr, tmp_path, caplog):
    video = movie_without_english(arr)

    def on_search(params):
        if params["language"] == "en":
            pause(arr, "podnapisi", "ServiceUnavailable", "in 25 minutes")

    arr.on_search = on_search
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert languages(arr) == ["da", "en"] and arr.translate_calls == []
    assert "Engelsk-søgningen for Film 1 tæller ikke: venter: Podnapisi er sat på pause" in text
    row = db.get("movie:1")
    assert row["search_at"] and row["en_search_at"] is None and row["search_started_at"] is None
    # stadig på pause: ingen ny søgning
    arr.on_search = None
    _, text = run(r, caplog)
    assert languages(arr) == ["da", "en"]
    assert "der søges ikke efter engelsk til 1 emner" in text
    # tilbage: ny, gyldig søgning efter engelsk, som finder engelsk -> oversættes
    unpause(arr)
    arr.on_search = lambda params: add_english(arr, video)
    arr.on_translate = write_haiku(video)
    run(r, caplog)
    assert languages(arr) == ["da", "en", "en"] and len(arr.translate_calls) == 1


def test_english_searches_count_towards_max_per_round(arr, tmp_path, caplog):
    movie_without_english(arr)
    r, db, _ = runner(arr, tmp_path, max_searches_per_round=1)
    _, text = run(r, caplog)
    assert languages(arr) == ["da"]
    assert "Film 1 - venter: højst 1 søgninger pr. runde" in text
    run(r, caplog)
    assert languages(arr) == ["da", "en"]


def test_dry_run_starts_no_english_search(arr, tmp_path, caplog):
    movie_without_english(arr)
    r, db, _ = runner(arr, tmp_path, dry_run=True)
    db.upsert("movie:1", media_type="movie", media_id=1, status="soegt", search_at=iso())
    _, text = run(r, caplog)
    assert arr.search_calls == [] and arr.translate_calls == [] and arr.writes == []
    assert "DRY_RUN: ville søge nu efter engelsk til Film 1 (ingen dansk og ingen engelsk kilde)" in text
    assert db.get("movie:1")["status"] == "soegt"


# --- mærker -------------------------------------------------------------------------------
def test_movie_with_radarr_tag_searches_and_translates(arr, tmp_path):
    video, _ = movie_with_english(arr)
    arr.seerr_requests.clear()
    arr.movies[1]["tags"] = [3]
    arr.on_translate = write_haiku(video)
    r, db, _ = runner(arr, tmp_path)
    r.run_once()
    assert len(arr.search_calls) == 1 and len(arr.translate_calls) == 1
    assert db.get("movie:1")["status"] == "faerdig"


def test_episode_of_tagged_series_searches_episode(arr, tmp_path):
    video = episode_with_english(arr)

    def go(form):
        write_haiku(video)(form)

    arr.on_translate = go
    r, db, _ = runner(arr, tmp_path)
    r.run_once()
    assert arr.search_calls == [
        {
            "path": "/api/episodes/subtitles",
            "language": "da",
            "forced": "False",
            "hi": "False",
            "seriesid": "50",
            "episodeid": "5",
        }
    ]
    assert arr.translate_calls[0]["type"] == "episode" and db.get("episode:5")["status"] == "faerdig"


def test_item_without_seerr_and_without_tag_is_ignored(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.seerr_requests.clear()
    episode_with_english(arr, tags=(1,))
    r, db, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert arr.search_calls == [] and arr.translate_calls == []
    assert "Film 1 - ikke Seerr-ønske og mangler mærket oversaet" in text
    assert "Serie 50 1x05 - ikke Seerr-ønske og mangler mærket oversaet" in text


def test_max_searches_per_round(arr, tmp_path, caplog):
    for i in range(1, 4):
        arr.add_movie(i, 100 + i, {"days": 1}, [])
    r, db, _ = runner(arr, tmp_path, max_searches_per_round=2)
    _, text = run(r, caplog)
    assert [c["radarrid"] for c in arr.search_calls] == ["1", "2"]
    assert "Film 3 - venter: højst 2 søgninger pr. runde" in text


# --- synkronisering ved nye mærker ---------------------------------------------------------
def test_new_tagged_movies_trigger_one_radarr_sync(arr, tmp_path, caplog):
    arr.add_movie(1, 101, {"days": 1}, [], requested=False, tags=[3])
    arr.add_movie(2, 102, {"days": 1}, [], requested=False, tags=[3])
    arr.add_movie(3, 103, {"days": 1}, [], requested=False)
    arr.bazarr_unknown |= {"movie:1", "movie:2", "movie:3"}
    r, _, _ = runner(arr, tmp_path)
    _, text = run(r, caplog)
    assert arr.sync_calls == ["update_movies"]
    assert "Beder Bazarr synkronisere med Radarr (nyt med mærket oversaet: Film 1, Film 2)" in text
    # Bazarr kender dem nu -> ingen ny synkronisering
    arr.bazarr_unknown.clear()
    run(r, caplog)
    assert arr.sync_calls == ["update_movies"]


def test_new_episode_of_tagged_series_triggers_sonarr_sync(arr, tmp_path):
    episode_with_english(arr)
    arr.add_episode(6, 50, 5000, {"minutes": 1}, [], tags=[7])
    arr.episodes[6]["new"] = True  # Sonarr har filen, Bazarr kender den ikke endnu
    r, _, _ = runner(arr, tmp_path)
    r.run_once()
    assert arr.sync_calls == ["update_series"]


def test_known_tagged_items_do_not_sync(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 1}, [], requested=False, tags=[3])
    episode_with_english(arr)
    r, _, _ = runner(arr, tmp_path)
    r.run_once()
    assert arr.sync_calls == []


def test_dry_run_neither_searches_nor_syncs(arr, tmp_path, caplog):
    movie_with_english(arr)
    arr.add_movie(2, 102, {"days": 1}, [], requested=False, tags=[3])
    arr.bazarr_unknown.add("movie:2")
    r, db, _ = runner(arr, tmp_path, dry_run=True)
    _, text = run(r, caplog)
    assert arr.search_calls == [] and arr.sync_calls == [] and arr.translate_calls == [] and arr.writes == []
    assert "DRY_RUN: ville bede Bazarr synkronisere med Radarr nu (nyt med mærket oversaet: Film 2)" in text
    assert "DRY_RUN: ville søge nu efter dansk til Film 1 (Seerr, ingen gyldig søgning endnu)" in text
    assert db.get("movie:1") is None


# --- gamle .env-nøgler og databaseopgradering --------------------------------------------------
def test_old_env_keys_logged_once_at_start(arr, tmp_path, caplog):
    from oversaetter.config import Config

    cfg = Config.from_env({"SEERR_DELAY_MINUTES": "30", "TAG_DELAY_HOURS": "48", "DATA_DIR": str(tmp_path)})
    r = Runner(cfg, Database(str(tmp_path / "x.db")), http=arr.client(), claude=object())
    with caplog.at_level("INFO", logger="oversaetter"):
        r.log_start()
    assert caplog.text.count("SEERR_DELAY_MINUTES i .env bruges ikke længere og ignoreres") == 1
    assert caplog.text.count("TAG_DELAY_HOURS i .env bruges ikke længere og ignoreres") == 1
    assert "FIRST_SEEN_DELAY_HOURS" not in caplog.text


def test_database_upgrade_keeps_rows(tmp_path):
    path = str(tmp_path / "gammel.db")
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE items (key TEXT PRIMARY KEY, media_type TEXT NOT NULL, media_id INTEGER NOT NULL, title TEXT,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, reason TEXT, video_path TEXT, source TEXT,
            da_path TEXT, started_at TEXT, updated_at TEXT, cost_usd REAL NOT NULL DEFAULT 0, flags_before INTEGER,
            flags_after INTEGER, fixes INTEGER);
        CREATE TABLE spend (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, month TEXT NOT NULL,
            item_key TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL NOT NULL);
        INSERT INTO items (key, media_type, media_id, title, status, attempts) VALUES
            ('movie:1', 'movie', 1, 'Gammel film', 'faerdig', 1),
            ('episode:2', 'episode', 2, 'Gammelt afsnit', 'forsoeg_igen', 1);
        INSERT INTO spend (ts, month, cost_usd) VALUES ('2026-10-01T00:00:00+00:00', '2026-10', 0.5);
        """
    )
    old.commit()
    old.close()
    db = Database(path)
    assert db.get("movie:1")["status"] == "faerdig" and db.get("movie:1")["search_at"] is None
    assert db.get("episode:2")["title"] == "Gammelt afsnit" and db.get("episode:2")["search_started_at"] is None
    assert db.month_spend("2026-10") == 0.5
    db.upsert("episode:2", search_at=iso())
    assert Database(path).get("episode:2")["search_at"]  # anden åbning: ingen fejl, data bevaret
    assert Database(path).track_paused(["subdl"]).keys() == {"subdl"}
