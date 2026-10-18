"""Trin B4 (18.10.2026): hørehæmmede-tekster (HI/SDH) som kilde - rensning, talernavne som note og hi_rest.

Alle tekster er selvskrevne. tests/hi_sample.en.srt er en syntetisk HI-tekst.
"""

import json
import logging
import os
import shutil

import pytest

from oversaetter import hi, ordliste
from oversaetter.checks import check_cue
from oversaetter.oversaet import SYSTEM_PROMPT_V4, build_user, run_translate_test
from oversaetter.srt import read_file
from tests.fakes import FakeArr, en_sub
from tests.helpers import cfg, cue
from tests.test_direct import runner as direct_runner
from tests.test_forbedringer import SmartClaude
from tests.test_oversaet import request_payload

SAMPLE = os.path.join(os.path.dirname(__file__), "hi_sample.en.srt")


def cleaned(*texts):
    return hi.clean([cue(i, i + 0.9, t) for i, t in enumerate(texts)])


def one(text):
    r = cleaned(text)
    return r.cues[0].text if r.cues else None


# --- 1) rensningen -------------------------------------------------------------------------------------
def test_sound_alone_drops_cue():
    r = cleaned("[DOOR SLAMS]", "Hello.")
    assert [c.text for c in r.cues] == ["Hello."]
    assert r.stats.sounds == 1 and r.stats.dropped == 1


@pytest.mark.parametrize(
    "src, out",
    [
        ("(whispers) Come here.", "Come here."),
        ("Come here. [DOOR SLAMS]", "Come here."),
        ("Wait (sighs), what?", "Wait, what?"),
        ("(laughs nervously) Sure.", "Sure."),
        ("[speaking foreign language] Hola.", "Hola."),
        ("(MUSIC PLAYING) Hey.", "Hey."),
        ("<i>(whispers) Come here.</i>", "<i>Come here.</i>"),
    ],
)
def test_sound_descriptions_are_removed(src, out):
    assert one(src) == out


@pytest.mark.parametrize("src", ["We're (almost) there.", "(ikke) nu", "Vi er (næsten) der", "Room (2) is free."])
def test_ordinary_parentheses_are_kept(src):
    assert one(src) == src


@pytest.mark.parametrize("src", ["♪ ... ♪", "♪", "#", "# Happy birthday to you #", "♪ La la love ♪"])
def test_music_lines_drop_cue(src):
    r = cleaned(src)
    assert r.cues == [] and r.stats.music == 1 and r.stats.dropped == 1


def test_music_over_two_lines_and_mixed_cue():
    r = cleaned("♪ Oh, the night is long\nand the road is cold ♪", "♪ La la ♪\nHey, you.")
    assert [c.text for c in r.cues] == []  # trin B4d: én sanglinje dropper hele replikken
    assert r.stats.music == 4
    r = hi.clean([cue(0, 1, "♪ La la ♪\nHey, you.")], drop_lyrics=False)
    assert [c.text for c in r.cues] == ["Hey, you."]


def test_hash_number_is_not_music():
    assert one("#1 fan") == "#1 fan"


def test_speaker_name_is_removed_and_kept():
    r = cleaned("RICK: Let's go.")
    assert r.cues[0].text == "Let's go." and r.speakers == ["Rick"]
    assert r.notes == ["[Rick] Let's go."]


def test_two_speakers_keep_dashes():
    r = cleaned("- RICK: Go.\n- CORA: No.")
    assert r.cues[0].text == "- Go.\n- No."
    assert r.speakers == ["Rick", "Cora"]
    assert r.notes[0] == "- [Rick] Go.\n- [Cora] No."


def test_multi_word_speaker():
    r = cleaned("MAN ON RADIO: Copy.")
    assert r.cues[0].text == "Copy." and r.speakers == ["Man on radio"]


def test_shouted_line_is_not_a_speaker():
    r = cleaned("WE'RE NOT ALONE.")
    assert r.cues[0].text == "WE'RE NOT ALONE." and r.speakers == [] and r.stats.speakers == 0


@pytest.mark.parametrize("src", ["OK: fine.", "Rick: hello.", "It's 10:30."])
def test_not_speaker_names(src):
    assert one(src) == src


def test_italics_are_kept():
    r = cleaned("<i>CORA: Did you hear that?</i>", "<i>[SIREN]\nThey're coming.</i>")
    assert [c.text for c in r.cues] == ["<i>Did you hear that?</i>", "<i>They're coming.</i>"]
    assert r.notes[0] == "<i>[Cora] Did you hear that?</i>"


def test_removed_line_in_two_speaker_cue_drops_dash():
    assert one("- [GASPS]\n- What was that?") == "What was that?"


def test_log_once_per_film_and_not_when_zero(caplog):
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        hi.clean(read_file(SAMPLE))
        hi.clean([cue(1, 2, "Hello.")])
    lines = [r.message for r in caplog.records if r.message.startswith("HI-rensning")]
    assert lines == ["HI-rensning: 3 lydbeskrivelser, 3 musiklinjer, 5 talernavne, 3 replikker droppet"]


def test_sample_file():
    r = hi.clean(read_file(SAMPLE))
    assert [c.text for c in r.cues] == [
        "Come here.",
        "We're (almost) there.",
        "Let's go.",
        "- Go.\n- No.",
        "Copy.",
        "WE'RE NOT ALONE.",
        "<i>Did you hear that?</i>",
        "What was that?",
    ]
    assert r.speakers == ["Rick", "Cora", "Man on radio"]
    assert r.cues[2].start == 9500  # tidskoderne følger med


# --- 2) talernavne til modellen og hi_rest -------------------------------------------------------------
def test_model_input_has_speaker_note():
    r = cleaned("RICK: Let's go.", "- RICK: Go.\n- CORA: No.")
    en = [c.text for c in r.cues]
    payload = json.loads(build_user(en, [None, None], 0, 2, [], r.notes).split("\n\n", 1)[1])
    assert payload["lines"][0]["en"] == "[Rick] Let's go."
    assert payload["lines"][1]["en"] == "- [Rick] Go.\n- [Cora] No."


def test_prompt_v4_explains_speaker_note():
    assert "Et navn i [klammer] foran en linje fortæller, hvem der taler." in SYSTEM_PROMPT_V4
    assert "Skriv aldrig navnet eller klammerne i den danske tekst." in SYSTEM_PROMPT_V4


@pytest.mark.parametrize("da", ["[Rick] Kom så.", "RICK: Kom så.", "- Ja.\n- CORA: Nej.", "Kom så. [Rick]"])
def test_hi_rest_flags_leftovers(da):
    assert "hi_rest" in check_cue(da, "Let's go.", 42)


@pytest.mark.parametrize("da", ["Kom så.", "- Gå.\n- Nej.", "VI ER IKKE ALENE.", "Klokken er 10:30."])
def test_hi_rest_ok(da):
    assert "hi_rest" not in check_cue(da, "Let's go.", 42)


# --- 3) ordlisten --------------------------------------------------------------------------------------
def test_glossary_gets_speakers():
    sent = {}

    def ask(system, user, schema):
        sent["user"] = user
        raise RuntimeError("stop")

    r = hi.clean(read_file(SAMPLE))
    ordliste.build(
        ask,
        [c.text for c in r.cues],
        "Cast (actor - character - gender):\n- A - Rick - male",
        None,
        None,
        r.speakers,
        r.notes,
    )
    assert "Speakers named in the subtitles" in sent["user"] and "Rick, Cora, Man on radio" in sent["user"]
    assert "Cast (actor - character - gender)" in sent["user"] and "[Rick] Let's go." in sent["user"]


def test_glossary_without_speakers_is_unchanged():
    sent = {}

    def ask(system, user, schema):
        sent["user"] = user
        raise RuntimeError("stop")

    ordliste.build(ask, ["Hello."], None, None)
    assert "Speakers" not in sent["user"] and "English subtitles (one cue per line):\nHello." in sent["user"]


# --- 4) hele oversaet-test-flowet ----------------------------------------------------------------------
def fake_translate(leak=False):
    def translate(kwargs):
        lines = []
        for x in request_payload(kwargs)["lines"]:
            da = f"Dansk {x['i']}"
            if x["en"].count("- ") >= 2:  # følger prompt v4: streger i den engelske replik giver streger på dansk
                da = f"- Dansk {x['i']}\n- Dansk {x['i']}"
            if leak and x["en"].startswith("[Rick]"):
                da = "[Rick] Kom så."
            lines.append({"i": x["i"], "da": da})
        return json.dumps({"lines": lines}, ensure_ascii=False)

    return translate


def run_test(tmp_path, hi_on, leak=False, prompt="v4"):
    en = str(tmp_path / "film.en.srt")
    shutil.copy(SAMPLE, en)
    fake = SmartClaude(translate=fake_translate(leak))
    out = str(tmp_path / "ud.da.srt")
    rc = run_translate_test(
        cfg(),
        en,
        out,
        "anthropic",
        "claude-opus-5-5",
        batch=4,
        client=fake,
        prompt=prompt,
        glossary=True,
        hi_clean=hi_on,
    )
    return rc, fake, out


def test_full_flow_hi_on(tmp_path, capsys):
    rc, fake, out = run_test(tmp_path, True)
    assert rc == 0
    report = capsys.readouterr().out
    assert "HI-rensning: 3 lydbeskrivelser, 3 musiklinjer, 5 talernavne, 3 replikker droppet" in report
    assert "talere: Rick, Cora, Man on radio" in report and "hi on" in report
    sent = [x["en"] for req in fake.of("oversæt") for x in request_payload(req)["lines"]]
    assert "[Rick] Let's go." in sent and "- [Rick] Go.\n- [Cora] No." in sent
    assert not any("[DOOR" in x or "♪" in x or "RICK:" in x for x in sent)
    assert "Rick, Cora, Man on radio" in fake.of("ordliste")[0]["messages"][0]["content"]
    assert len(read_file(out)) == 8
    assert "Rettelsesflag fra kontrollen: 0" in report


def test_full_flow_hi_off(tmp_path, capsys):
    rc, fake, out = run_test(tmp_path, False)
    assert rc == 0
    report = capsys.readouterr().out
    assert "HI-rensning: slået fra" in report and "hi off" in report
    sent = [x["en"] for req in fake.of("oversæt") for x in request_payload(req)["lines"]]
    assert "[DOOR SLAMS]" in sent and "RICK: Let's go." in sent
    assert "Speakers" not in fake.of("ordliste")[0]["messages"][0]["content"]
    assert len(read_file(out)) == 11


def test_full_flow_flags_leak_as_hi_rest(tmp_path, capsys):
    rc, _, _ = run_test(tmp_path, True, leak=True)
    assert rc == 0
    assert "hi_rest=1" in capsys.readouterr().out


def test_prompt_v3_gets_clean_text_without_notes(tmp_path, capsys):
    rc, fake, _ = run_test(tmp_path, True, prompt="v3")
    assert rc == 0
    sent = [x["en"] for req in fake.of("oversæt") for x in request_payload(req)["lines"]]
    assert "Let's go." in sent and not any("[Rick]" in x for x in sent)


# --- 5) driften ----------------------------------------------------------------------------------------
def test_drift_cleans_hi_source_before_batches(tmp_path):
    arr = FakeArr(str(tmp_path / "Media"))
    video = arr.add_movie(1, 101, {"days": 2}, [], tags=[3])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    shutil.copy(SAMPLE, en_path)
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    claude = SmartClaude(translate=fake_translate())
    r, db = direct_runner(arr, tmp_path, claude=claude, prompt_version="v4", glossary=True)
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig"
    sent = [x["en"] for req in claude.of("oversæt") for x in request_payload(req)["lines"]]
    assert "[Rick] Let's go." in sent and not any("[DOOR" in x or "♪" in x for x in sent)
    assert "Rick, Cora, Man on radio" in claude.of("ordliste")[0]["messages"][0]["content"]
    uploaded = arr.uploads[-1]["content"]
    uploaded = uploaded.decode() if isinstance(uploaded, bytes) else uploaded
    assert "[" not in uploaded and "RICK" not in uploaded and "♪" not in uploaded
