"""Trin B4e (18.10.2026): små rettelser efter test21.

Alle tekster er selvskrevne.
"""

from oversaetter import hi, ordliste
from oversaetter.checks import check_cue
from oversaetter.oversaet import SYSTEM_PROMPT_V4
from tests.helpers import cue


def cleaned(*texts):
    return hi.clean([cue(i, i + 0.9, t) for i, t in enumerate(texts)])


# --- 1) replikker uden bogstaver eller tal droppes ---------------------------------------------------------
def test_cue_without_letters_or_digits_is_dropped():
    r = cleaned('" "', "...", "-", "<i> </i>", "Go.", "2.")
    assert [c.text for c in r.cues] == ["Go.", "2."]
    assert r.stats.dropped == 4 and "4 replikker droppet" in r.stats.text()


# --- 2) mål omregnes til metriske enheder ------------------------------------------------------------------
def test_imperial_units_skip_number_check():
    assert "missing_number" not in check_cue("Otte kilometer, cirka.", "Five miles, give or take.", 42)
    assert "missing_number" not in check_cue("Otte kilometer, cirka.", "5 miles, give or take.", 42)
    assert "missing_number" not in check_cue("Det er 38 grader.", "It's 100 °F.", 42)
    assert "missing_number" in check_cue("Otte kilometer.", "5 kilometers.", 42)


def test_prompt_v4_converts_to_metric():
    assert "miles -> kilometer, feet -> meter, pounds -> kilo, Fahrenheit -> Celsius" in SYSTEM_PROMPT_V4
    assert '"five miles" -> "otte kilometer"' in SYSTEM_PROMPT_V4


# --- 3) danske stavemåder i ordlisten ----------------------------------------------------------------------
def test_wordlist_prompt_has_danish_spellings():
    for part in ("Taliban -> Taleban", "Hezbollah -> Hizbollah", "Al Qaeda -> al-Qaeda", "Tehran -> Teheran", '"Tali"'):
        assert part in ordliste.PROMPT


# --- 4) Shit. -> Pis. --------------------------------------------------------------------------------------
def test_prompt_v4_shit_is_pis():
    assert '"Shit." or "Oh, shit." as a cue on its own -> "Pis." / "Åh, pis."' in SYSTEM_PROMPT_V4
