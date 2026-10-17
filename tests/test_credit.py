from oversaetter.checks import EnglishIndex, check_all
from oversaetter.config import Config
from oversaetter.credit import apply_credit
from oversaetter.postprocess import postprocess
from oversaetter.srt import compose, parse
from tests.helpers import FakeBudget, FakeClaude, cfg, cue, response

CREDIT = "Oversat af Claude for ≋"
HEADER = "# Subtitles translated with Lingarr # "


def test_replaces_bazarr_header_with_room():
    cues = [cue(1, 5, HEADER), cue(10, 12, "Hej")]
    out, what = apply_credit(cues, CREDIT)
    assert [c.text for c in out] == [CREDIT, "Hej"]
    assert (out[0].start, out[0].end) == (1000, 6000)  # højst 5 sek.
    assert "erstattet" in what


def test_credit_ends_half_a_second_before_first_line():
    cues = [cue(0, 3.5, HEADER), cue(3.5, 5, "Hej")]  # Bazarr lægger headeren fra 0 når der er lidt plads
    out, _ = apply_credit(cues, CREDIT)
    assert out[0].text == CREDIT
    assert (out[0].start, out[0].end) == (1000, 3000)


def test_too_little_room_drops_credit_and_header():
    cues = [cue(0, 2.8, HEADER), cue(2.8, 4, "Hej")]  # kun 1,3 sek. plads
    out, what = apply_credit(cues, CREDIT)
    assert [c.text for c in out] == ["Hej"]
    assert "udeladt" in what


def test_no_header_inserts_and_renumbers():
    cues = [cue(8, 9, "Første"), cue(10, 11, "Anden")]
    out, what = apply_credit(cues, CREDIT)
    assert [c.text for c in out] == [CREDIT, "Første", "Anden"]
    assert "indsat" in what
    text = compose(out)
    assert text.startswith("1\n00:00:01,000 --> 00:00:06,000\n" + CREDIT)
    assert "\n3\n00:00:10,000" in text
    assert len(parse(text)) == 3


def test_no_header_and_no_room():
    cues = [cue(0.5, 2, "Allerede i gang")]
    out, _ = apply_credit(cues, CREDIT)
    assert [c.text for c in out] == ["Allerede i gang"]


def test_exactly_two_seconds_is_enough():
    cues = [cue(3.5, 5, "Hej")]  # 1,0 -> 3,0 = 2 sek.
    out, _ = apply_credit(cues, CREDIT)
    assert out[0].text == CREDIT and out[0].end == 3000


TWO_LINES = 'Oversat til dig af\\n<font color="#58D9AE">≋ </font><b>Stream<font color="#58D9AE">365</font></b>'
TWO_LINES_OUT = 'Oversat til dig af\n<font color="#58D9AE">≋ </font><b>Stream<font color="#58D9AE">365</font></b>'


def test_backslash_n_gives_two_lines_in_finished_srt():
    out, _ = apply_credit([cue(8, 9, "Hej")], TWO_LINES)
    text = compose(out)
    assert text.startswith("1\n00:00:01,000 --> 00:00:06,000\n" + TWO_LINES_OUT + "\n\n2\n")
    again = parse(text)
    assert again[0].text.split("\n") == ["Oversat til dig af", TWO_LINES_OUT.split("\n")[1]]
    assert [c.text for c in again[1:]] == ["Hej"]


def test_default_from_env_gives_two_lines():
    out, _ = apply_credit([cue(8, 9, "Hej")], Config.from_env({}).credit_text)
    assert out[0].text == TWO_LINES_OUT


def test_text_without_backslash_n_works_as_before():
    out, _ = apply_credit([cue(8, 9, "Hej")], CREDIT)
    assert out[0].text == CREDIT and "\n" not in out[0].text


def test_empty_credit_text_turns_credit_off():
    c = Config.from_env({"CREDIT_TEXT": ""})
    out, what = apply_credit([cue(1, 5, HEADER), cue(8, 9, "Hej")], c.credit_text)
    assert [x.text for x in out] == ["Hej"]
    assert what == "kreditlinje slået fra"
    assert apply_credit([cue(8, 9, "Hej")], "  \\n ")[0][0].text == "Hej"


def test_check_and_proofread_never_see_credit():
    en = [cue(1, 5, "Hello there, my friend."), cue(10, 12, "Where are you going?")]
    # En fil, der allerede har vores kreditlinje (fx kørt to gange), og en med Bazarrs header.
    for first in (TWO_LINES_OUT, HEADER):
        da = [cue(1, 5, first), cue(10, 12, "Hvor skal du ikkje hen?")]
        claude = FakeClaude([response([{"i": 1, "da": "Hvor skal du hen?"}])])
        res = postprocess(da, en, cfg(credit_text=TWO_LINES), claude, FakeBudget())
        assert res.flags_before == 1 and res.flags_after == 0 and res.remaining == {}
        assert [line["da"] for line in claude.lines()] == ["Hvor skal du ikkje hen?"]
        assert [c.text for c in res.cues] == [TWO_LINES_OUT, "Hvor skal du hen?"]
    # Den gratis kontrol flager heller ikke selve kreditlinjen
    assert check_all([cue(1, 5, TWO_LINES_OUT)], EnglishIndex(en), 42) == {}
