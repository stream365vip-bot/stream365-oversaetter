"""Trin B4c (18.10.2026): talerstreger, sangtekst, rensede tal og identiske fremmedsprogs-replikker (test18/test19).

Alle tekster er selvskrevne.
"""

import json

from oversaetter import hi
from oversaetter.checks import check_cue
from oversaetter.config import Config
from oversaetter.oversaet import SYSTEM_PROMPT_V4, run_translate_test
from oversaetter.srt import read_file, write_file_atomic
from tests.helpers import cfg, cue, srt
from tests.test_forbedringer import SmartClaude
from tests.test_oversaet import request_payload


def cleaned(*texts, drop_lyrics=True):
    return hi.clean([cue(i, i + 0.9, t) for i, t in enumerate(texts)], drop_lyrics=drop_lyrics)


# --- 1) talerstreger -----------------------------------------------------------------------------------
def test_two_speakers_get_dashes():
    r = cleaned("RICK: Go.\nCORA: No.")
    assert r.cues[0].text == "- Go.\n- No."
    assert r.speakers == ["Rick", "Cora"] and r.notes == ["- [Rick] Go.\n- [Cora] No."]


def test_same_speaker_or_no_speaker_gets_no_dashes():
    assert cleaned("RICK: Go.\nRICK: Now.").cues[0].text == "Go.\nNow."
    assert cleaned("Go.\nNow.").cues[0].text == "Go.\nNow."


def test_existing_dash_and_italics_are_kept():
    assert cleaned("<i>RICK: Go.</i>\n- CORA: No.").cues[0].text == "<i>- Go.</i>\n- No."


def test_danish_dashes_are_normalized_on_write(tmp_path):
    path = str(tmp_path / "ud.da.srt")
    cues = [cue(1, 2, "-Hej.\n-Hej."), cue(3, 4, "–Hej.\n<i>-Ja.</i>"), cue(5, 6, "Det er -5 grader.")]
    write_file_atomic(path, cues)
    assert [c.text for c in read_file(path)] == ["- Hej.\n- Hej.", "- Hej.\n<i>- Ja.</i>", "Det er -5 grader."]


def test_missing_dash():
    assert "missing_dash" in check_cue("Gå.\nNej.", "- Go.\n- No.", 42)
    assert "missing_dash" in check_cue("- Gå.\nNej.", "- Go.\n- No.", 42)
    assert "missing_dash" not in check_cue("- Gå.\n- Nej.", "- Go.\n- No.", 42)
    assert "missing_dash" not in check_cue("Gå.\nNej.", "Go.\nNo.", 42)


def test_prompt_v4_asks_for_dashes_and_checkpoint():
    assert 'Når den engelske replik har streger ("- "), skal alle danske linjer' in SYSTEM_PROMPT_V4
    assert '"checkpoint"/"checkpoints" -> "kontrolpost"/"kontrolposter"' in SYSTEM_PROMPT_V4


# --- 2) sangtekst --------------------------------------------------------------------------------------
def test_lyrics_are_dropped():
    r = cleaned("♪ Oh say can you see", "♪ THE ALL AMERICAN REJECTS: Staring Back At Me", "Hello.", "Oh say ♪")
    assert [c.text for c in r.cues] == ["Hello."]
    assert r.stats.music == 3 and r.stats.dropped == 3


def test_lyrics_line_in_mixed_cue():
    r = cleaned("♪ Oh say can you see\n- Where are we?")
    assert [c.text for c in r.cues] == ["Where are we?"]


def test_lyrics_kept_with_drop_lyrics_false():
    r = cleaned("♪ Oh say can you see", "♪ THE ALL AMERICAN REJECTS: Staring Back At Me", drop_lyrics=False)
    assert [c.text for c in r.cues] == ["♪ Oh say can you see", "♪ THE ALL AMERICAN REJECTS: Staring Back At Me"]
    assert cleaned("♪ La la ♪", drop_lyrics=False).cues == []  # som før B4c


def test_drop_lyrics_setting():
    assert Config().drop_lyrics is True
    assert Config.from_env({"DROP_LYRICS": "false"}).drop_lyrics is False


# --- 3) tal fra talernavne -----------------------------------------------------------------------------
def test_numbered_speaker_is_removed():
    r = cleaned("REPORTER 1: Kabul fell on August 15.")
    assert r.cues[0].text == "Kabul fell on August 15." and r.speakers == ["Reporter 1"]


def test_no_missing_number_from_speaker_name():
    en = cleaned("REPORTER 1: Kabul fell on August 15.").cues[0].text
    assert "missing_number" not in check_cue("Kabul faldt den 15. august.", en, 42)
    assert "missing_number" in check_cue("Kabul faldt i august.", en, 42)


# --- 4) identical_to_english ---------------------------------------------------------------------------
def test_identical_foreign_line_is_not_flagged():
    for text in ("Salam alaikum.", "Salam alaikum. Wa alaikum assalam.", "Merci.", "Allahu Akbar!"):
        assert "identical_to_english" not in check_cue(text, text, 42)
    assert "identical_to_english" in check_cue("Let's go.", "Let's go.", 42)


# --- 5) checkpoint -------------------------------------------------------------------------------------
def test_checkpoint_is_english_word():
    assert "english_word" in check_cue("Vi når et checkpoint.", "We reach a checkpoint.", 42, strict=True)
    assert "english_word" in check_cue("Flere checkpoints.", "More checkpoints.", 42, strict=True)
    assert "english_word" not in check_cue("Vi når en kontrolpost.", "We reach a checkpoint.", 42, strict=True)


# --- hele oversaet-test-flowet ---------------------------------------------------------------------------
HI_FILE = srt(
    ("00:00:01,000", "00:00:03,000", "RICK: How long is our flight?\nCORA: I don't know."),
    ("00:00:04,000", "00:00:06,000", "♪ Oh say can you see"),
    ("00:00:07,000", "00:00:09,000", "♪ THE ALL AMERICAN REJECTS: Staring Back At Me"),
    ("00:00:10,000", "00:00:12,000", "REPORTER 1: Kabul fell on August 15."),
    ("00:00:13,000", "00:00:15,000", "RICK: Go.\nCORA: No."),
    ("00:00:16,000", "00:00:18,000", "Salam alaikum."),
)
ANSWERS = {
    "- [Rick] How long is our flight?\n- [Cora] I don't know.": "-Hvor lang er vores flyvning?\n–Det ved jeg ikke.",
    "[Reporter 1] Kabul fell on August 15.": "Kabul faldt den 15. august.",
    "- [Rick] Go.\n- [Cora] No.": "Gå.\nNej.",  # mangler streger: missing_dash
    "Salam alaikum.": "Salam alaikum.",
}


def test_full_flow_two_speakers_and_lyrics(tmp_path, capsys):
    en = tmp_path / "film.en.hi.srt"
    en.write_text(HI_FILE, encoding="utf-8")

    def translate(kwargs):
        lines = [{"i": x["i"], "da": ANSWERS[x["en"]]} for x in request_payload(kwargs)["lines"]]
        return json.dumps({"lines": lines}, ensure_ascii=False)

    fake = SmartClaude(translate=translate)
    out = str(tmp_path / "ud.da.srt")
    rc = run_translate_test(cfg(), str(en), out, "anthropic", "claude-opus-5-5", client=fake, prompt="v4")
    assert rc == 0
    report = capsys.readouterr().out
    sent = [x["en"] for req in fake.of("oversæt") for x in request_payload(req)["lines"]]
    assert sorted(sent) == sorted(ANSWERS)
    assert "2 musiklinjer" in report and "2 replikker droppet" in report
    assert "Rettelsesflag fra kontrollen: 1 (missing_dash=1)" in report
    assert [c.text for c in read_file(out)] == [
        "- Hvor lang er vores flyvning?\n- Det ved jeg ikke.",
        "Kabul faldt den 15. august.",
        "Gå.\nNej.",
        "Salam alaikum.",
    ]
