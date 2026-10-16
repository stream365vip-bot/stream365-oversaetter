import os

from oversaetter.srt import Cue, compose, format_time, parse, read_file, write_file_atomic


def test_parse_danish_letters_and_italics():
    text = (
        "1\n00:00:01,000 --> 00:00:02,500\n<i>Blåbærgrød på øen</i>\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nÆble\nØl og Å\n"
    )
    cues = parse(text)
    assert len(cues) == 2
    assert cues[0].text == "<i>Blåbærgrød på øen</i>"
    assert cues[0].start == 1000 and cues[0].end == 2500
    assert cues[1].lines == ["Æble", "Øl og Å"]


def test_parse_crlf_bom_and_no_trailing_newline():
    text = "﻿1\r\n00:00:01,000 --> 00:00:02,000\r\nHej\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nFarvel"
    cues = parse(text)
    assert [c.text for c in cues] == ["Hej", "Farvel"]


def test_parse_many_trailing_blank_lines_and_empty_cue():
    text = "1\n00:00:01,000 --> 00:00:02,000\n\n2\n00:00:03,000 --> 00:00:04,000\nTekst\n\n\n\n"
    cues = parse(text)
    assert len(cues) == 2
    assert cues[0].text == ""
    assert cues[1].text == "Tekst"


def test_number_inside_text_is_not_an_index():
    text = "1\n00:00:01,000 --> 00:00:02,000\nÅret var\n1984\n\n2\n00:00:03,000 --> 00:00:04,000\nNæste\n"
    cues = parse(text)
    assert cues[0].text == "Året var\n1984"


def test_roundtrip_and_renumbering():
    cues = [Cue(61_001, 62_000, "<i>Første</i>"), Cue(3_600_000, 3_601_500, "Anden\nlinje")]
    out = compose(cues)
    assert out.startswith("1\n00:01:01,001 --> 00:01:02,000\n<i>Første</i>\n\n2\n01:00:00,000")
    assert parse(out) == cues


def test_format_time():
    assert format_time(0) == "00:00:00,000"
    assert format_time(3_723_004) == "01:02:03,004"


def test_cp1252_fallback(tmp_path):
    p = tmp_path / "a.srt"
    p.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nRødgrød\n".encode("cp1252"))
    assert read_file(str(p))[0].text == "Rødgrød"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    p = tmp_path / "film.da.srt"
    p.write_text("gammel", encoding="utf-8")
    os.chmod(p, 0o640)
    write_file_atomic(str(p), [Cue(1000, 2000, "Ny æøå")])
    assert sorted(os.listdir(tmp_path)) == ["film.da.srt"]
    assert read_file(str(p))[0].text == "Ny æøå"
    assert (os.stat(p).st_mode & 0o777) == 0o640
