import os

import pytest

from oversaetter.clients import Bazarr, Radarr, Seerr, SeerrIndex, Sonarr
from oversaetter.db import Database
from oversaetter.selection import Selector, choose_source
from tests.fakes import FakeArr, embedded, en_sub
from tests.helpers import cfg as base_cfg

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


def make_cfg(tmp_path, **kw):
    settings = dict(
        bazarr_url="http://bazarr",
        bazarr_api_key="bazarr-key",
        radarr_url="http://radarr",
        sonarr_url="http://sonarr",
        seerr_url="http://seerr",
        data_dir=str(tmp_path / "data"),
        dry_run=False,
    )
    settings.update(kw)
    return base_cfg(**settings)


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def find(arr, tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    http = arr.client()
    db = Database(str(tmp_path / "data" / "test.db"))
    seerr = SeerrIndex(Seerr(cfg, http).requests())
    sel = Selector(cfg, Bazarr(cfg, http), Radarr(cfg, http), Sonarr(cfg, http), seerr, db)
    return sel.find()


def external_en(arr, video, name=".en.srt", **kw):
    path = os.path.splitext(video)[0] + name
    with open(path, "w", encoding="utf-8") as f:
        f.write(SRT)
    return en_sub(path, **kw)


def test_seerr_movie_waits_for_delay(arr, tmp_path):
    v = arr.add_movie(1, 101, {"minutes": 30}, [])
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    cands, stats = find(arr, tmp_path)
    assert cands == [] and stats.waiting == 1


def test_seerr_movie_after_delay_is_candidate(arr, tmp_path):
    v = arr.add_movie(1, 101, {"minutes": 61}, [])
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    cands, _ = find(arr, tmp_path)
    assert len(cands) == 1
    c = cands[0]
    assert (c.media_type, c.media_id, c.reason) == ("movie", 1, "Seerr")
    assert c.source_path.endswith(".en.srt") and c.da_path == os.path.splitext(v)[0] + ".da.srt"


def test_movie_not_requested_is_ignored(arr, tmp_path):
    v = arr.add_movie(1, 101, {"days": 10}, [], requested=False)
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    cands, stats = find(arr, tmp_path)
    assert cands == [] and stats.not_selected == 1


def test_declined_seerr_request_does_not_count(arr, tmp_path):
    v = arr.add_movie(1, 101, {"days": 10}, [])
    arr.seerr_requests[0]["status"] = 3
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    assert find(arr, tmp_path)[0] == []


def test_tagged_episode_waits_48_hours(arr, tmp_path):
    v = arr.add_episode(5, 50, 5000, {"hours": 47}, [], tags=[7])
    arr.episodes[5]["subtitles"] = [external_en(arr, v)]
    cands, stats = find(arr, tmp_path)
    assert cands == [] and stats.waiting == 1


def test_tagged_episode_after_48_hours(arr, tmp_path):
    v = arr.add_episode(5, 50, 5000, {"hours": 49}, [], tags=[7])
    arr.episodes[5]["subtitles"] = [external_en(arr, v)]
    cands, _ = find(arr, tmp_path)
    assert [(c.media_type, c.media_id, c.reason) for c in cands] == [("episode", 5, "mærke")]


def test_episode_with_other_tag_is_ignored(arr, tmp_path):
    v = arr.add_episode(5, 50, 5000, {"days": 9}, [], tags=[1])
    arr.episodes[5]["subtitles"] = [external_en(arr, v)]
    assert find(arr, tmp_path)[0] == []


def test_episode_requested_via_seerr_uses_seerr_delay(arr, tmp_path):
    v = arr.add_episode(6, 60, 6000, {"minutes": 90}, [], season=2)
    arr.episodes[6]["subtitles"] = [external_en(arr, v)]
    arr.seerr_requests.append({"type": "tv", "status": 2, "media": {"tvdbId": 6000}, "seasons": [{"seasonNumber": 2}]})
    cands, _ = find(arr, tmp_path)
    assert [c.reason for c in cands] == ["Seerr"]


def test_episode_in_season_not_requested_is_ignored(arr, tmp_path):
    v = arr.add_episode(6, 60, 6000, {"days": 3}, [], season=1)
    arr.episodes[6]["subtitles"] = [external_en(arr, v)]
    arr.seerr_requests.append({"type": "tv", "status": 2, "media": {"tvdbId": 6000}, "seasons": [{"seasonNumber": 2}]})
    assert find(arr, tmp_path)[0] == []


def test_already_has_embedded_danish(arr, tmp_path):
    v = arr.add_movie(1, 101, {"days": 2}, [])
    arr.movies[1]["subtitles"] = [external_en(arr, v), embedded("da", 9)]
    cands, stats = find(arr, tmp_path)
    assert cands == [] and "indlejret" in stats.skipped[0]


def test_already_has_danish_file_on_disk(arr, tmp_path):
    v = arr.add_movie(1, 101, {"days": 2}, [])
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    with open(os.path.splitext(v)[0] + ".da.hi.srt", "w") as f:
        f.write(SRT)
    cands, stats = find(arr, tmp_path)
    assert cands == [] and "dansk fil" in stats.skipped[0]


def test_no_english_source(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 2}, [embedded("sv", 3)])
    cands, stats = find(arr, tmp_path)
    assert cands == [] and "ingen engelsk" in stats.skipped[0]


def test_embedded_english_is_used_when_no_external(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    cands, _ = find(arr, tmp_path)
    assert cands[0].subtitles_id == 42 and cands[0].source_path is None


def test_choose_source_prefers_external_non_hi_and_skips_forced(tmp_path):
    files = {}
    for name in ("a.en.hi.srt", "a.en.srt", "a.en.forced.srt"):
        p = tmp_path / name
        p.write_text(SRT)
        files[name] = str(p)
    subs = [
        en_sub(files["a.en.hi.srt"], hi=True),
        en_sub(files["a.en.forced.srt"], forced=True),
        en_sub(files["a.en.srt"]),
        embedded("en", 5),
    ]
    path, sub_id, _ = choose_source(subs)
    assert path == files["a.en.srt"] and sub_id is None
    path, _, _ = choose_source([en_sub(files["a.en.hi.srt"], hi=True), embedded("en", 5)])
    assert path == files["a.en.hi.srt"]


def test_finished_items_are_not_picked_again(arr, tmp_path):
    v = arr.add_movie(1, 101, {"days": 2}, [])
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    db = Database(str(tmp_path / "data" / "test.db"))
    for status in ("faerdig", "fejlet", "ikke_muligt"):
        db.upsert("movie:1", media_type="movie", media_id=1, status=status)
        cands, stats = find(arr, tmp_path)
        assert cands == [] and stats.done_before == 1
    db.upsert("movie:1", status="forsoeg_igen")
    assert len(find(arr, tmp_path)[0]) == 1


def test_few_api_calls_for_many_wanted_items(arr, tmp_path):
    for i in range(1, 30):
        arr.add_movie(i, 100 + i, {"days": 2}, [], requested=False)
        arr.add_episode(i, 500 + i, 9000 + i, {"days": 9}, [], tags=[1])
    cands, stats = find(arr, tmp_path)
    assert cands == [] and stats.not_selected == 58
    assert arr.radarr_calls == 1  # alle film hentes i ét kald
    assert arr.sonarr_episode_calls == 0  # serier uden mærke/ønske slår ikke afsnit op
