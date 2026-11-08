"""Trin C1 (8.11.2026): alt nyt oversættes (TRANSLATE_ALL_SINCE) og nye titler uden sprogprofil får profilen i
Bazarr - men kun når Bazarrs egen søgning efter manglende tekster står på "Manually". Alt med fakes; intet netværk.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from oversaetter.config import Config
from tests.fakes import FakeArr, ago, en_sub
from tests.test_direct import EN, FakeAnthropic, runner

SINCE = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d")


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def new_episode(arr, ep_id, series_id=60, hours_ago=2, profile=False):
    """Et afsnit uden Seerr-ønske og uden mærke, importeret for hours_ago timer siden."""
    video = arr.add_episode(ep_id, series_id, 600 + series_id, {"hours": hours_ago}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[ep_id]["subtitles"] = [en_sub(en_path)]
    arr.sonarr_history.append(
        {"episodeId": ep_id, "seriesId": series_id, "date": ago(hours=hours_ago), "sourceTitle": f"Serie.S01E{ep_id}"}
    )
    if not profile:
        arr.no_profile.add(f"series:{series_id}")
    return video


def run(arr, tmp_path, rounds=2, **kw):
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), **kw)
    for _ in range(rounds):
        r.run_once()
    return r, db


def messages(caplog):
    return [rec.getMessage() for rec in caplog.records]


# --- indstillingen -------------------------------------------------------------------------------
def test_config_reads_date_and_rejects_nonsense():
    cfg = Config.from_env({"TRANSLATE_ALL_SINCE": "2026-11-10"})
    assert cfg.all_since() == datetime(2026, 11, 10, tzinfo=UTC)
    assert Config.from_env({}).all_since() is None
    with pytest.raises(ValueError, match="TRANSLATE_ALL_SINCE"):
        Config.from_env({"TRANSLATE_ALL_SINCE": "på tirsdag"})


def test_api_settings_from_env():
    cfg = Config.from_env({"API_PORT": "8090", "API_TOKEN": " hemmelig "})
    assert cfg.api_port == 8090 and cfg.api_token == "hemmelig"


# --- alt nyt ---------------------------------------------------------------------------------------
def test_off_by_default_new_episode_is_not_chosen(arr, tmp_path):
    video = new_episode(arr, 5, profile=True)
    r, db = run(arr, tmp_path)
    assert not os.path.exists(os.path.splitext(video)[0] + ".da.srt")
    assert arr.profile_sets == [] and db.get("episode:5") is None


def test_new_episode_without_profile_gets_profile_and_is_translated(arr, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="oversaetter")
    video = new_episode(arr, 5)
    r, db = run(arr, tmp_path, translate_all_since=SINCE)
    assert arr.profile_sets == [("series", 60, 1)]
    assert os.path.exists(os.path.splitext(video)[0] + ".da.srt")
    assert db.get("episode:5")["status"] == "faerdig"
    assert any("Serie 60 fik sprogprofilen Dansk" in m for m in messages(caplog))
    assert any(c.get("episodeid") == "5" and c.get("language") == "da" for c in arr.search_calls)


def test_import_before_the_date_is_not_new(arr, tmp_path):
    video = new_episode(arr, 5, hours_ago=24 * 5, profile=True)
    run(arr, tmp_path, translate_all_since=SINCE)
    assert not os.path.exists(os.path.splitext(video)[0] + ".da.srt")


def test_no_profiles_while_bazarr_searches_by_itself(arr, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="oversaetter")
    arr.wanted_freq = (6, 876000)
    video = new_episode(arr, 5)
    r, db = run(arr, tmp_path, rounds=3, translate_all_since=SINCE)
    assert arr.profile_sets == []
    assert not os.path.exists(os.path.splitext(video)[0] + ".da.srt")
    warnings = [m for m in messages(caplog) if "Bazarr søger selv" in m]
    assert len(warnings) == 1 and "serier" in warnings[0] and "Manually" in warnings[0]  # kun én gang
    assert all("hemmelig" not in m for m in messages(caplog))  # nøgler fra indstillingerne logges aldrig


def test_titles_with_profile_still_work_while_bazarr_searches_by_itself(arr, tmp_path):
    arr.wanted_freq = (6, 6)
    video = new_episode(arr, 5, profile=True)
    run(arr, tmp_path, translate_all_since=SINCE)
    assert os.path.exists(os.path.splitext(video)[0] + ".da.srt")


def test_missing_profile_name_gives_warning(arr, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="oversaetter")
    new_episode(arr, 5)
    run(arr, tmp_path, translate_all_since=SINCE, bazarr_profile="Findes ikke")
    assert arr.profile_sets == []
    assert any("findes ikke i Bazarr" in m for m in messages(caplog))


def test_dry_run_sets_no_profile(arr, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="oversaetter")
    new_episode(arr, 5)
    run(arr, tmp_path, translate_all_since=SINCE, dry_run=True)
    assert arr.profile_sets == []
    assert any("DRY_RUN: ville give Serie 60 sprogprofilen" in m for m in messages(caplog))


def test_new_movie_without_profile(arr, tmp_path):
    video = arr.add_movie(9, 909, {"hours": 1}, [], requested=False)
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.movies[9]["subtitles"] = [en_sub(en_path)]
    arr.no_profile.add("movie:9")
    arr.radarr_history.append({"movieId": 9, "date": ago(hours=1), "sourceTitle": "Film.9.2026"})
    r, db = run(arr, tmp_path, translate_all_since=SINCE)
    assert arr.profile_sets == [("movie", 9, 1)]
    assert db.get("movie:9")["status"] == "faerdig"


def test_old_episodes_of_the_series_are_not_chosen(arr, tmp_path):
    """Serien får profil, så Bazarr melder også et gammelt afsnit som manglende - det er ikke nyt og ikke ønsket."""
    new_episode(arr, 5)
    old = arr.add_episode(1, 60, 660, {"days": 400}, [])
    run(arr, tmp_path, translate_all_since=SINCE)
    assert not os.path.exists(os.path.splitext(old)[0] + ".da.srt")
    assert not any(c.get("episodeid") == "1" for c in arr.search_calls)


def test_new_file_after_finished_is_handled_again(arr, tmp_path, caplog):
    """En opgradering (ny fil) efter en færdig oversættelse oversættes forfra."""
    caplog.set_level(logging.INFO, logger="oversaetter")
    video = new_episode(arr, 5, hours_ago=5)
    r, db = run(arr, tmp_path, translate_all_since=SINCE)
    assert db.get("episode:5")["status"] == "faerdig"
    # Sonarr skifter filen ud: den danske tekst forsvinder, og historikken får en ny import
    da = os.path.splitext(video)[0] + ".da.srt"
    os.remove(da)
    arr.episodes[5]["subtitles"] = [s for s in arr.episodes[5]["subtitles"] if s.get("code2") != "da"]
    arr.sonarr_history.append({"episodeId": 5, "seriesId": 60, "date": ago(seconds=-60), "sourceTitle": "ny"})
    for _ in range(2):
        r.run_once()
    assert os.path.exists(da) and db.get("episode:5")["status"] == "faerdig"
    assert any("Ny fil til episode:5" in m for m in messages(caplog))


def test_history_error_does_not_stop_the_round(arr, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="oversaetter")
    arr.add_movie(1, 101, {"days": 2}, [])  # Seerr-ønske kører videre
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), translate_all_since=SINCE)
    arr.fail["sonarr"] = 500  # Sonarr svarer med fejl
    r.run_once()
    assert any("Kunne ikke hente nye importer fra Radarr/Sonarr" in m for m in messages(caplog))
    assert any("Runde:" in m for m in messages(caplog))
