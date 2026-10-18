"""Trin B3 (18.10.2026): engelske bandeord i ordlisten, ordspil (mixed_case), faste oversættelser og en mindre
følsom inconsistent_term.

Alle tekster er selvskrevne.
"""

import json
from types import SimpleNamespace

import pytest

from oversaetter import ordliste
from oversaetter.checks import check_cue
from oversaetter.oversaet import SYSTEM_PROMPT_V4, run_translate_test
from tests.helpers import cfg
from tests.test_forbedringer import FakeTmdb, SmartClaude, write_en
from tests.test_oversaet import request_payload


def flags(da, en="", glossary=None):
    return check_cue(da, en, 42, None, 2000, glossary, strict=True)


def wl(*terms):
    return SimpleNamespace(terms=[{"en": en, "da": da, "note": ""} for en, da in terms])


# --- 1) ordlisten fritager kun egennavne ---------------------------------------------------------------
def test_glossary_slang_kept_in_english_is_still_flagged():
    assert "english_word" in flags("Hvad så, bitches?", "What's up, bitches?", wl(("bitches", "bitches")))


def test_glossary_proper_name_is_not_flagged():
    g = wl(("The Paragon", "The Paragon"))
    assert "english_word" not in flags("Velkommen til The Paragon.", "Welcome to The Paragon.", g)
    assert "english_word" in flags("Velkommen til The Paragon, bitches.", "Welcome to The Paragon, bitches.", g)
    assert "english_word" in flags("Det er the end.", "That's the end.", g)  # kun selve navnet fritages


@pytest.mark.parametrize(
    "da",
    [
        "Holy shit.",
        "Damn!",
        "For helvede, hell!",
        "What the hell?",
        "Gosh.",
        "Jeez.",
        "Seriously?",
        "Literally.",
        "Kom, babe.",
        "Hej, sweetie.",
        "Hej, honey.",
        "Din bastard.",
        "Sikke en asshole.",
        "Han er en jerk.",
        "Loser!",
        "I er losers.",
    ],
)
def test_new_english_words_are_flagged(da):
    assert "english_word" in flags(da)


@pytest.mark.parametrize("da", ["Åh, shit!", "Shit, mand.", "Det er helt i orden.", "Hellere i dag."])
def test_allowed_words_are_not_flagged(da):
    assert "english_word" not in flags(da)


def test_glossary_prompt_demands_danish_slang():
    for part in (
        "bitch/bitches -> kælling/kællinger",
        "dude -> mand",
        "Swear words, exclamations and filler words are NEVER terms",  # trin B4d: holy shit er ikke en term
        'never marked "keep"',
    ):
        assert part in ordliste.PROMPT


# --- 2) ordspil og mixed_case --------------------------------------------------------------------------
def test_mixed_case_flags_made_up_word():
    assert "mixed_case" in flags("Man kan ikke stave til sundhedsTHC uden THC.")


@pytest.mark.parametrize(
    "da",
    [
        "THC er lovligt.",
        "JD's bil.",
        "Min iPhone.",
        "Køb det på eBay.",
        "Det er sundheds-THC.",
        "Hun er på OnlyFans.",
        "Tjek AccuWeather.",
        "Se det på YouTube.",
    ],
)
def test_mixed_case_exemptions(da):
    assert "mixed_case" not in flags(da)


def test_prompt_v4_wordplay_and_fixed_translations():
    for part in (
        "Ordspil og bogstavlege",
        "Opfind aldrig halvengelske eller sammensatte volapyk-ord",
        '"frequent-flyer miles" -> "bonuspoint"',
        '"Who gives a shit?" -> "Hvem fanden går op i det?"',
        '"Farvel så længe"',
        '"alle sammen" is always two words',
    ):
        assert part in SYSTEM_PROMPT_V4


# --- 4) inconsistent_term ------------------------------------------------------------------------------
@pytest.mark.parametrize("da", ["Det er reglerne.", "Det er reglen.", "Det er en regel.", "Det er regler."])
def test_inflection_counts_as_same_term(da):
    assert "inconsistent_term" not in flags(da, "That's the rule.", wl(("the rule", "reglen")))


def test_other_word_is_flagged():
    assert "inconsistent_term" in flags("Det er loven.", "That's the rule.", wl(("the rule", "reglen")))


def test_repetition_in_same_cue_is_not_flagged():
    g = wl(("the green one", "den grønne"))
    assert "inconsistent_term" not in flags("Den grønne! Ja, den grønne.", "The green one! The green one.", g)


# --- hele oversaet-test-flowet -------------------------------------------------------------------------
def test_full_test_flow_is_green_with_fake_answers(tmp_path, capsys):
    cues = [(f"00:00:{i:02d},000", f"00:00:{i + 1:02d},900", f"Line {chr(55 + i)}.") for i in range(10, 30, 2)]
    en = write_en(tmp_path, cues)
    key = tmp_path / "tmdb.txt"
    key.write_text("v3-nøgle")

    def translate(kwargs):
        lines = [{"i": x["i"], "da": f"Dansk {x['i']}"} for x in request_payload(kwargs)["lines"]]
        return json.dumps({"lines": lines}, ensure_ascii=False)

    fake = SmartClaude(translate=translate)
    rc = run_translate_test(
        cfg(tmdb_api_key_file=str(key)),
        en,
        str(tmp_path / "ud.srt"),
        "anthropic",
        "claude-opus-5-5",
        batch=3,
        client=fake,
        prompt="v4",
        context=True,
        glossary=True,
        tmdb_id="603",
        parallel=4,
        http=FakeTmdb().client(),
    )
    assert rc == 0
    report = capsys.readouterr().out
    assert "Rettelsesflag fra kontrollen: 0" in report
    assert not fake.of("ret")
