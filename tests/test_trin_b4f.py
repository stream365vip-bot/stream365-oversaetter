"""Trin B4f (18.10.2026): rettelser efter første rigtige film i drift (indlejret SDH-spor).

Alle tekster er selvskrevne.
"""

import json
import logging
import os

import pytest

from oversaetter import hi
from oversaetter.__main__ import forget
from oversaetter.db import Database
from oversaetter.oversaet import drop_empty, translate_all
from tests.fakes import FakeArr, en_sub
from tests.helpers import cue, srt
from tests.test_direct import FakeAnthropic, da_text, payload, reply, runner
from tests.test_selection import make_cfg


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def cleaned(*texts):
    return hi.clean([cue(i, i + 0.9, t) for i, t in enumerate(texts)])


# --- 1) alt i [klammer] er lydbeskrivelse ------------------------------------------------------------------
def test_square_brackets_are_always_sounds():
    r = cleaned("[ Clicking ]", "[ Sheridan groans ]", "[ Paper rustling ]", "[Phones ringing]", "Hello.")
    assert [c.text for c in r.cues] == ["Hello."]
    assert r.stats.sounds == 4 and r.stats.dropped == 4


def test_bracket_line_in_two_line_cue_removes_dash():
    assert cleaned("- [ Door slams ]\n- Come in.").cues[0].text == "Come in."
    assert cleaned("Wait [ Sheridan groans ], what?").cues[0].text == "Wait, what?"


def test_parentheses_keep_old_rule():
    assert cleaned("(whispers) Go.").cues[0].text == "Go."
    assert cleaned("We're (almost) there.").cues[0].text == "We're (almost) there."


# --- 2) uploadet er aldrig HI ------------------------------------------------------------------------------
def test_upload_sends_hi_false_for_hi_source(arr, tmp_path):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.sdh.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(srt(("00:00:10,000", "00:00:12,000", "[ Clicking ]\nWhere is the car?")))
    arr.movies[1]["subtitles"] = [en_sub(en_path, hi=True)]
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert db.get("movie:1")["en_hi"] == 1
    up = arr.uploads[0]
    assert up["hi"] == "false" and up["filename"] == "Film 1.da.srt"


# --- 3) tomt svar på en replik med kun klammer/parenteser/musik --------------------------------------------
def test_only_markup():
    for text in ("[ Music ]", "(Theme song plays)", "♪", "- (Theme song)\n- [ Music ]", "<i>(Theme song)</i>"):
        assert hi.only_markup(text), text
    for text in ("Go.", "...", "We're (almost) there.", "(2) Go."):
        assert not hi.only_markup(text), text


def test_empty_answer_on_bracket_only_line_is_dropped_not_english(caplog):
    en = ["[ Music ]", "Where is the car?"]

    def translate(kwargs):
        lines = [{"i": x["i"], "da": "" if x["i"] == 1 else "Hvor er bilen?"} for x in payload(kwargs)["lines"]]
        return reply(json.dumps({"lines": lines}))

    fake = FakeAnthropic(translate=translate)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = translate_all(
            en, "anthropic", fake, "claude-opus-5-5", "medium", 50, 60.0, empty_ok=hi.markup_positions(en)
        )
    assert len(fake.requests) == 1  # intet genforsøg
    assert res.fallback == [] and res.da == ["", "Hvor er bilen?"]
    assert drop_empty(res.da, res.fallback) == ([1], [])
    assert "beholder den engelske tekst" not in caplog.text


def test_empty_answer_on_paren_only_line_in_direct_mode(arr, tmp_path, caplog):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(
            srt(
                ("00:00:10,000", "00:00:12,000", "(Theme song plays)"),
                ("00:00:13,000", "00:00:15,000", "Where is the car?"),
            )
        )
    arr.movies[1]["subtitles"] = [en_sub(en_path)]

    def translate(kwargs):
        lines = [{"i": x["i"], "da": "" if x["i"] == 1 else "Hvor er bilen?"} for x in payload(kwargs)["lines"]]
        return reply(json.dumps({"lines": lines}))

    claude = FakeAnthropic(translate=translate)
    r, db = runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    text = da_text(video)
    assert "Hvor er bilen?" in text and "Theme" not in text
    assert claude.kinds() == ["oversæt"]
    assert db.get("movie:1")["status"] == "faerdig"
    assert "beholdt engelsk: 0" in caplog.text


def test_empty_fix_on_paren_only_line_deletes_it(arr, tmp_path, caplog):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(
            srt(
                ("00:00:10,000", "00:00:12,000", "(Theme song plays)"),
                ("00:00:13,000", "00:00:15,000", "Where is the car?"),
                ("00:00:16,000", "00:00:18,000", "Come with me, please."),
            )
        )
    arr.movies[1]["subtitles"] = [en_sub(en_path)]

    def translate(kwargs):
        da = {1: "Temasang, please.", 2: "Hvor er bilen?", 3: "Kom med mig, please."}
        return reply(json.dumps({"lines": [{"i": x["i"], "da": da[x["i"]]} for x in payload(kwargs)["lines"]]}))

    fixes = [{"i": 1, "da": ""}, {"i": 3, "da": ""}]  # 1: kun parentes på engelsk -> slet; 3: almindelig -> kasseret
    claude = FakeAnthropic(translate=translate, fix=lambda kw: reply(json.dumps({"fixes": fixes})))
    r, db = runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    text = da_text(video)
    assert "Temasang" not in text and "Hvor er bilen?" in text and "Kom med mig, please." in text
    assert "1 replikker med kun lydbeskrivelse/musik er slettet" in caplog.text
    assert "Rettelse kasseret: replik 3: tom tekst" in caplog.text
    assert db.get("movie:1")["status"] == "faerdig"


# --- 4) glem ------------------------------------------------------------------------------------------------
class FakeRadarr:
    def __init__(self, movies):
        self.movies = movies

    def by_tmdb(self, tmdb_id):
        return [m for m in self.movies if m["tmdbId"] == tmdb_id]


def _db(tmp_path):
    db = Database(str(tmp_path / "oversaetter.db"))
    db.upsert("movie:7", media_type="movie", media_id=7, title="Idiots", status="faerdig", glossary='{"terms": []}')
    db.upsert("movie:8", media_type="movie", media_id=8, title="Other", status="faerdig")
    return db


def test_forget_by_title_removes_row(tmp_path, caplog):
    db = _db(tmp_path)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert forget(make_cfg(tmp_path), "idiots", db=db) == 0
    assert db.get("movie:7") is None and db.get("movie:8") is not None
    assert "Glemt: Idiots (movie:7, status faerdig, forsøg 0, med ordliste)" in caplog.text


def test_forget_by_tmdb_id(tmp_path):
    db = _db(tmp_path)
    assert forget(make_cfg(tmp_path), "4242", db=db, radarr=FakeRadarr([{"id": 7, "tmdbId": 4242}])) == 0
    assert db.get("movie:7") is None and db.get("movie:8") is not None


def test_forget_unknown_deletes_nothing(tmp_path, caplog):
    db = _db(tmp_path)
    assert forget(make_cfg(tmp_path), "Nope", db=db) == 1
    assert forget(make_cfg(tmp_path), "999", db=db, radarr=FakeRadarr([])) == 1
    assert db.get("movie:7") is not None and db.get("movie:8") is not None
    assert "intet slettet" in caplog.text
