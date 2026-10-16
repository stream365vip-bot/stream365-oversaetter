from oversaetter.credit import apply_credit
from oversaetter.srt import compose, parse
from tests.helpers import cue

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
