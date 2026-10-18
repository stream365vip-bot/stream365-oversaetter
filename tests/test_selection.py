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
        # De ældre tests dækker det gamle flow (TRANSLATE_MODE=lingarr). Det direkte flow testes i test_direct.py.
        translate_mode="lingarr",
    )
    settings.update(kw)
    return base_cfg(**settings)


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def find(arr, tmp_path, searched=(), **kw):
    """Kør udvælgelsen. searched: nøgler der allerede har haft en gyldig søgning.

    Returnerer (alle valgte emner: klar til oversættelse + mangler søgning, stats).
    """
    cfg = make_cfg(tmp_path, **kw)
    http = arr.client()
    db = Database(str(tmp_path / "data" / "test.db"))
    for key in searched:
        kind, ident = key.split(":")
        db.upsert(key, media_type=kind, media_id=int(ident), status="soegt", search_at="2026-10-17T10:00:00+00:00")
    seerr = SeerrIndex(Seerr(cfg, http).requests())
    sel = Selector(cfg, Bazarr(cfg, http), Radarr(cfg, http), Sonarr(cfg, http), seerr, db)
    ready, to_search, stats = sel.find()
    find.ready, find.to_search = ready, to_search
    return ready + to_search, stats


def external_en(arr, video, name=".en.srt", **kw):
    path = os.path.splitext(video)[0] + name
    with open(path, "w", encoding="utf-8") as f:
        f.write(SRT)
    return en_sub(path, **kw)


def test_new_seerr_movie_needs_search_first(arr, tmp_path):
    v = arr.add_movie(1, 101, {"minutes": 1}, [])
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    cands, stats = find(arr, tmp_path)
    assert find.ready == [] and [c.key for c in find.to_search] == ["movie:1"]
    assert cands[0].reason == "Seerr" and stats.waiting == 0


def test_seerr_movie_after_valid_search_is_candidate(arr, tmp_path):
    v = arr.add_movie(1, 101, {"minutes": 1}, [])
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    find(arr, tmp_path, searched=["movie:1"])
    assert find.to_search == [] and len(find.ready) == 1
    c = find.ready[0]
    assert (c.media_type, c.media_id, c.reason) == ("movie", 1, "Seerr")
    assert c.source_path.endswith(".en.srt") and c.da_path == os.path.splitext(v)[0] + ".da.srt"


def test_movie_not_requested_is_ignored(arr, tmp_path):
    v = arr.add_movie(1, 101, {"days": 10}, [], requested=False)
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    cands, stats = find(arr, tmp_path)
    assert cands == [] and stats.not_selected == 1
    assert stats.examples[0][1] == "Film 1 - ikke Seerr-ønske og mangler mærket oversaet"


def test_movie_with_radarr_tag_is_selected(arr, tmp_path):
    v = arr.add_movie(1, 101, {"minutes": 1}, [], requested=False, tags=[3])
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    cands, _ = find(arr, tmp_path)
    assert [(c.key, c.reason) for c in find.to_search] == [("movie:1", "mærke")]


def test_declined_seerr_request_does_not_count(arr, tmp_path):
    v = arr.add_movie(1, 101, {"days": 10}, [])
    arr.seerr_requests[0]["status"] = 3
    arr.movies[1]["subtitles"] = [external_en(arr, v)]
    assert find(arr, tmp_path)[0] == []


def test_tagged_episode_needs_no_waiting(arr, tmp_path):
    v = arr.add_episode(5, 50, 5000, {"minutes": 1}, [], tags=[7])
    arr.episodes[5]["subtitles"] = [external_en(arr, v)]
    find(arr, tmp_path)
    assert [(c.key, c.reason, c.series_id) for c in find.to_search] == [("episode:5", "mærke", 50)]
    find(arr, tmp_path, searched=["episode:5"])
    assert [(c.media_type, c.media_id, c.reason) for c in find.ready] == [("episode", 5, "mærke")]


def test_episode_with_other_tag_is_ignored(arr, tmp_path):
    v = arr.add_episode(5, 50, 5000, {"days": 9}, [], tags=[1])
    arr.episodes[5]["subtitles"] = [external_en(arr, v)]
    assert find(arr, tmp_path)[0] == []


def test_episode_requested_via_seerr(arr, tmp_path):
    v = arr.add_episode(6, 60, 6000, {"minutes": 1}, [], season=2)
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
    cands, stats = find(arr, tmp_path, searched=["movie:1"])
    # ingen dansk og ingen engelsk kilde: ikke sprunget over, men Bazarr skal søge efter engelsk
    assert cands == [] and stats.skipped == []
    assert "Film 1 - ingen engelsk tekstkilde - søger efter engelsk" in [t for _, t in stats.examples]


def test_embedded_english_is_used_when_no_external(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    cands, _ = find(arr, tmp_path, searched=["movie:1"])
    assert cands[0].subtitles_id == 42 and cands[0].source_path is None


def test_choose_source_prefers_external_hi_and_skips_forced(tmp_path):
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
    assert path == files["a.en.hi.srt"] and sub_id is None
    path, _, _ = choose_source(subs, prefer_hi=False)  # efterbehandlingens reference: som før
    assert path == files["a.en.srt"]
    path, _, _ = choose_source([en_sub(files["a.en.srt"]), embedded("en", 5)])
    assert path == files["a.en.srt"]


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
    assert arr.radarr_calls == 2  # alle film hentes i ét kald, mærkerne i ét
    assert arr.sonarr_episode_calls == 0  # serier uden mærke/ønske slår ikke afsnit op
