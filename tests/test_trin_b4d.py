"""Trin B4d (18.10.2026): rettelser efter test20 (The Fix, HI).

Alle tekster er selvskrevne.
"""

import json

from oversaetter import hi, ordliste
from oversaetter.checks import check_cue
from oversaetter.oversaet import SYSTEM_PROMPT_V4, run_translate_test
from tests.helpers import cfg, cue
from tests.test_forbedringer import GLOSSARY_REPLY, FakeTmdb, SmartClaude, write_en


def cleaned(*texts, drop_lyrics=True):
    return hi.clean([cue(i, i + 0.9, t) for i, t in enumerate(texts)], drop_lyrics=drop_lyrics)


# --- 1) talerstreger uden streger i kilden ---------------------------------------------------------------
def test_question_and_answer_without_names_is_unchanged():
    assert cleaned("How long is our flight?\nOh, I don't know.").cues[0].text == (
        "How long is our flight?\nOh, I don't know."
    )


def test_one_named_line_gets_dashes():
    r = cleaned("TUCKER: Go.\nNo.")
    assert r.cues[0].text == "- Go.\n- No." and r.notes == ["- [Tucker] Go.\n- No."]
    assert cleaned("Go.\nTUCKER: No.").cues[0].text == "- Go.\n- No."


def test_one_named_sentence_over_two_lines_gets_no_dashes():
    assert cleaned("TUCKER: I think we\nshould go now.").cues[0].text == "I think we\nshould go now."


def test_missing_dash_only_when_english_has_dashes():
    assert "missing_dash" not in check_cue("Hvor lang er flyveturen?\nAner det ikke.", "How long?\nNo idea.", 42)
    assert "missing_dash" in check_cue("Gå.\nNej.", cleaned("TUCKER: Go.\nNo.").cues[0].text, 42)


def test_prompt_v4_dashes_without_source_dashes():
    assert "også selv om den engelske ikke har streger. Er du i tvivl, så lad være." in SYSTEM_PROMPT_V4
    assert "never added otherwise" not in SYSTEM_PROMPT_V4


# --- 2) sangtekst ----------------------------------------------------------------------------------------
def test_one_lyrics_line_drops_whole_cue():
    r = cleaned("♪ Twinkle, twinkle, little star\nand bright stars", "Staring back at me #", "Hello.")
    assert [c.text for c in r.cues] == ["Hello."]
    assert r.stats.dropped == 2 and r.stats.music == 3


def test_lyrics_line_kept_with_drop_lyrics_false():
    r = cleaned("♪ Twinkle, twinkle\nand bright stars", drop_lyrics=False)
    assert [c.text for c in r.cues] == ["♪ Twinkle, twinkle\nand bright stars"]


# --- 3) ordlisten uden bandeord og udbrud ----------------------------------------------------------------
def test_swear_terms_are_filtered_from_glossary():
    reply = {
        "terms": [
            {"en": "shit", "da": "pis", "note": "bandeord"},
            {"en": "Holy shit!", "da": "Hold da kæft!", "note": ""},
            {"en": "okay", "da": "okay", "note": ""},
            {"en": "Tucker", "da": "Tucker", "note": "navn"},
            {"en": "bitch", "da": "kælling", "note": "skældsord"},
        ],
        "address": [],
        "tone": "",
    }
    assert [t["en"] for t in ordliste.parse(json.dumps(reply)).terms] == ["Tucker", "bitch"]
    assert "Swear words, exclamations and filler words are NEVER terms" in ordliste.PROMPT


# --- 4) tal med tusindtalsseparator ----------------------------------------------------------------------
def test_thousands_separator_is_same_number():
    for da in ("Der er 82.000 af dem.", "Der er 82 000 af dem.", "Der er 82000 af dem."):
        assert "missing_number" not in check_cue(da, "There are 82,000 of them.", 42)
    assert "missing_number" not in check_cue("Der er 82,000.", "There are 82.000.", 42)
    assert "missing_number" in check_cue("Der er 28.000 af dem.", "There are 82,000 of them.", 42)


# --- 5) faste oversættelser ------------------------------------------------------------------------------
def test_fixed_translations_in_prompt_and_english_word():
    for text in (
        '"lifer" -> "karrieresoldat"',
        '"Copy." (on the radio) -> "Modtaget."',
        '"Jesus." (an exclamation)',
        '"Flight deck to cockpit" -> "Flydæk til cockpit"',
        '"Jøsses." or "Åh gud."',
    ):
        assert text in SYSTEM_PROMPT_V4
    for da, en in (
        ("Jesus.", "Jesus."),
        ("- Jesus!", "- Jesus!"),
        ("Han er lifer.", "He's a lifer."),
        ("Copy.", "Copy."),
        ("Flight deck til cockpit.", "Flight deck to cockpit."),
    ):
        assert "english_word" in check_cue(da, en, 42, strict=True), da
    for da, en in (
        ("Jøsses.", "Jesus."),
        ("Han er karrieresoldat.", "He's a lifer."),
        ("Modtaget.", "Copy."),
        ("Flydæk til cockpit.", "Flight deck to cockpit."),
        ("Jesus elskede dem.", "Jesus loved them."),
    ):
        assert "english_word" not in check_cue(da, en, 42, strict=True), da


# --- 6) oversaet-test --vis-kontekst ---------------------------------------------------------------------
def _run(tmp_path, show_context):
    key = tmp_path / "tmdb.txt"
    key.write_text("nøgle")
    en = write_en(tmp_path)
    return run_translate_test(
        cfg(tmdb_api_key_file=str(key)),
        en,
        str(tmp_path / f"ud{show_context}.srt"),
        "anthropic",
        "claude-opus-5-5",
        client=SmartClaude(),
        prompt="v4",
        context=True,
        glossary=True,
        tmdb_id="603",
        http=FakeTmdb().client(),
        show_context=show_context,
    )


def test_report_shows_context_and_glossary(tmp_path, capsys):
    assert _run(tmp_path, True) == 0
    report = capsys.readouterr().out
    head = report.split("Engelsk fil:")[0]
    assert "--- Filmkontekst ---" in head and "--- Ordliste ---" in head
    assert '- "Big Mike" -> "Store Mike" (øgenavn)' in head
    assert "Tiltaler (1):\n- Tom til Big Mike: du" in head
    assert f"Termer ({len(GLOSSARY_REPLY['terms'])}):" in head
    ctx = head.split("--- Filmkontekst ---\n")[1].split("\n--- Ordliste ---")[0]
    assert ctx and ctx != "(ingen)"


def test_report_without_switch_has_no_context(tmp_path, capsys):
    assert _run(tmp_path, False) == 0
    assert "--- Filmkontekst ---" not in capsys.readouterr().out
