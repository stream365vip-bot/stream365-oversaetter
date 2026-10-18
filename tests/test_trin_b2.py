"""Trin B2 (18.10.2026): informationsflag, flere engelske ord, tomme fyldlyde uden genforsøg og prompt-cache.

Alle tekster er selvskrevne.
"""

import json
import logging
import os
from types import SimpleNamespace

import pytest

from oversaetter import oversaet
from oversaetter.checks import check_cue, fix_flags
from oversaetter.config import Config, model_price
from oversaetter.oversaet import SYSTEM_PROMPT_V4, run_translate_test, translate_all
from tests.fakes import FakeArr, en_sub
from tests.helpers import cfg, srt
from tests.test_direct import runner as direct_runner
from tests.test_forbedringer import FakeTmdb, SmartClaude, write_en
from tests.test_oversaet import FakeProvider, request_payload


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def flags(da, en="", duration_ms=2000, speed=None):
    return check_cue(da, en, 42, None, duration_ms, None, strict=True, reading_speed_max=speed)


# --- 1) reading_speed ----------------------------------------------------------------------------------
def test_reading_speed_limit_is_25_by_default():
    assert Config().reading_speed_max == 25.0
    assert Config.from_env({"READING_SPEED_MAX": "30"}).reading_speed_max == 30.0
    da = "x" * 24 + " " + "y" * 23  # 48 tegn
    assert "reading_speed" not in flags(da, "Short.", duration_ms=2000)  # 24 tegn/s
    da = "x" * 26 + " " + "y" * 25  # 52 tegn
    assert "reading_speed" in flags(da, "Short.", duration_ms=2000)  # 26 tegn/s
    assert "reading_speed" not in flags(da, "Short.", duration_ms=2000, speed=30)


def test_reading_speed_is_never_sent_to_fix_round():
    assert fix_flags({0: ["reading_speed"], 1: ["reading_speed", "english_word"], 2: ["empty"]}) == {
        1: ["english_word"],
        2: ["empty"],
    }


def test_drift_counts_reading_speed_but_does_not_fix_it(arr, tmp_path, caplog):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(
            srt(
                ("00:00:10,000", "00:00:11,000", "Go."),  # dansk 30 tegn på 1 sekund
                ("00:00:12,000", "00:00:14,000", "What's up?"),
                ("00:00:15,000", "00:00:17,000", "Fine."),
            )
        )
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    danish = {"Go.": "Gå nu, gå nu, gå nu, gå nu nu!", "What's up?": "Hvad så, bitches?", "Fine.": "Fint."}

    def translate(kwargs):
        lines = [{"i": x["i"], "da": danish[x["en"]]} for x in request_payload(kwargs)["lines"]]
        return json.dumps({"lines": lines}, ensure_ascii=False)

    claude = SmartClaude(translate=translate)
    r, db = direct_runner(arr, tmp_path, claude=claude, prompt_version="v4")
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    fix = request_payload(claude.of("ret")[0])["lines"]
    assert {x["i"] for x in fix if x.get("fix")} == {2}  # kun bitches - ikke læsehastigheden
    assert all("characters per second" not in " ".join(x.get("flag", [])) for x in fix)
    assert db.get("movie:1")["flags_before"] == 1
    done = next(rec.message for rec in caplog.records if rec.message.startswith("Færdig:"))
    assert "flag: 1, informationsflag: 1" in done


def test_only_reading_speed_gives_no_fix_round(arr, tmp_path):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(srt(("00:00:10,000", "00:00:11,000", "Go.")))
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    claude = SmartClaude(translate=lambda kw: json.dumps({"lines": [{"i": 1, "da": "Gå nu, gå nu, gå nu, gå nu nu!"}]}))
    r, db = direct_runner(arr, tmp_path, claude=claude, prompt_version="v4")
    r.run_once()
    assert claude.of("ret") == []
    assert db.get("movie:1")["status"] == "faerdig"


# --- 2) english_word -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "da",
    [
        "Hvad så, bitches?",
        "Din Bitch!",
        "I er nogle nobodies.",
        "Dude, hvad laver du?",
        "Hele crew'et er her.",
        "Kom så, bossman.",
        "Mr. Vice venter.",
        "Man, det er koldt.",
        "Tak, sir.",
    ],
)
def test_english_slang_is_flagged(da):
    assert "english_word" in flags(da)


@pytest.mark.parametrize(
    "da",
    ["Slap af, mand.", "Tak, bro.", "Nice.", "Okay.", "Det er cool.", "God vibe.", "Man ved aldrig.", "Kom, man."],
)
def test_danish_and_allowed_words_are_not_flagged(da):
    assert "english_word" not in flags(da)


def test_title_is_flagged_even_if_glossary_keeps_the_name():
    wl = SimpleNamespace(terms=[{"en": "Mr. Vice", "da": "Mr. Vice", "note": "navn"}])
    assert "english_word" in check_cue("Mr. Vice venter.", "Mr. Vice is waiting.", 42, None, 2000, wl, strict=True)


def test_prompt_v4_has_fixed_slang_list_and_rules():
    for part in (
        '"bitches" -> "kællinger"',
        '"bossman" -> "chefen"',
        '"Mr."/"Mrs." -> "hr."/"fru"',
        '"keep shooting" (with a gun) -> "bliv ved med',
        '"Åh gud"',
        "Herregud",
        '"Bestemt ikke."',
        '"Nej, slet ikke."',
    ):
        assert part in SYSTEM_PROMPT_V4


# --- 3) tomme fyldlyde ---------------------------------------------------------------------------------
def empty_for(target: str):
    def answer(kwargs):
        lines = [
            {"i": x["i"], "da": "" if x["en"] == target else f"Dansk {x['i']}"}
            for x in request_payload(kwargs)["lines"]
        ]
        return json.dumps({"lines": lines})

    return answer


def test_empty_answer_for_uh_gives_no_retry_and_is_dropped(tmp_path, capsys):
    cues = [("00:00:01,000", "00:00:02,000", "Hello."), ("00:00:03,000", "00:00:04,000", "Uh."),
            ("00:00:05,000", "00:00:06,000", "Bye.")]  # fmt: skip
    en = write_en(tmp_path, cues)
    fake = FakeProvider("anthropic", empty_for("Uh."))
    out = tmp_path / "ud.srt"
    assert run_translate_test(cfg(), en, str(out), "anthropic", "m", client=fake, prompt="v4") == 0
    assert len(fake.requests) == 1
    assert "00:00:03,000" not in out.read_text() and out.read_text().count("-->") == 2
    assert "heraf 1 med kun fyldlyde fjernet" in capsys.readouterr().out


@pytest.mark.parametrize("word", ["Oh.", "Huh?", "Ooh.", "Mm-hmm.", "Ugh!"])
def test_interjections_may_be_empty(word):
    en = ["Hello.", word]
    fake = FakeProvider("anthropic", empty_for(word))
    res = translate_all(en, "anthropic", fake, "m", None, 50, 60.0, empty_ok=oversaet.filler_positions(en, "v4"))
    assert len(fake.requests) == 1 and res.da == ["Dansk 1", ""] and res.fallback == []


def test_left_out_filler_also_gives_no_retry():
    def answer(kwargs):
        return json.dumps({"lines": [{"i": 1, "da": "Hej."}]})  # nummer 2 ("Uh.") mangler helt

    en = ["Hello.", "Uh."]
    fake = FakeProvider("anthropic", answer)
    res = translate_all(en, "anthropic", fake, "m", None, 50, 60.0, empty_ok=oversaet.filler_positions(en, "v4"))
    assert len(fake.requests) == 1 and res.da == ["Hej.", ""]


def test_empty_answer_for_real_words_is_retried():
    en = ["Hello.", "Get out."]
    fake = FakeProvider("anthropic", empty_for("Get out."))
    res = translate_all(en, "anthropic", fake, "m", None, 50, 60.0, empty_ok=oversaet.filler_positions(en, "v4"))
    assert len(fake.requests) > 1 and res.fallback == [1]


# --- 4) prompt-cache -----------------------------------------------------------------------------------
class CachingClaude:
    """Falsk Anthropic-klient, der svarer med cache-tokens i usage."""

    def __init__(self):
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        first = len(self.requests) == 1
        lines = [{"i": x["i"], "da": f"Dansk {x['i']}"} for x in request_payload(kwargs)["lines"]]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps({"lines": lines}))],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=200,
                cache_creation_input_tokens=3000 if first else 0,
                cache_read_input_tokens=0 if first else 3000,
            ),
        )


def test_cache_tokens_are_priced_correctly():
    # Opus 5.5: 4 USD ind, 20 USD ud pr. million. Cache-læs 10 %, cache-skriv 125 %.
    price = model_price("claude-opus-5-5", 1000, 200, cache_read=3000, cache_write=2000)
    assert price == pytest.approx((1000 * 4 + 3000 * 0.4 + 2000 * 5 + 200 * 20) / 1_000_000)
    assert Config().price(1000, 200, 3000, 2000) == pytest.approx(price)

    used = []
    fake = CachingClaude()
    res = translate_all(
        [f"Line {i}." for i in range(4)], "anthropic", fake, "claude-opus-5-5", None, 2, 60.0,
        on_usage=lambda *a: used.append(a), cache=True,
    )  # fmt: skip
    assert fake.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert used == [("claude-opus-5-5", 1000, 200, 0, 3000), ("claude-opus-5-5", 1000, 200, 3000, 0)]
    assert (res.input_tokens, res.cache_read, res.cache_write) == (2000, 3000, 3000)


def test_prompt_cache_off_sends_plain_system():
    fake = CachingClaude()
    translate_all(["Hi."], "anthropic", fake, "m", None, 50, 60.0, system=SYSTEM_PROMPT_V4, cache=False)
    assert fake.requests[0]["system"] == SYSTEM_PROMPT_V4
    assert Config.from_env({"PROMPT_CACHE": "false"}).prompt_cache is False and Config().prompt_cache is True


def test_drift_cost_uses_cache_prices(arr, tmp_path):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(srt(("00:00:01,000", "00:00:03,000", "Hello."), ("00:00:04,000", "00:00:06,000", "Bye.")))
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    fake = CachingClaude()
    r, db = direct_runner(arr, tmp_path, claude=fake)
    r.run_once()
    assert len(fake.requests) == 1 and fake.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    expected = model_price("claude-opus-5-5", 1000, 200, cache_write=3000)
    assert db.month_spend() == pytest.approx(expected)


# --- hele oversaet-test-flowet -------------------------------------------------------------------------
def test_full_test_flow_v4_context_glossary_parallel_reports_info_flags_separately(tmp_path, capsys):
    cues = [(f"00:00:{i:02d},000", f"00:00:{i:02d},900", f"Line {chr(55 + i)}.") for i in range(10, 19)]
    cues[1] = ("00:00:11,000", "00:00:11,500", "Go.")  # læsehastighed
    cues[4] = ("00:00:14,000", "00:00:14,900", "What's up?")  # bitches
    cues[6] = ("00:00:16,000", "00:00:16,900", "Uh.")  # fyldlyd, tom
    en = write_en(tmp_path, cues)
    key = tmp_path / "tmdb.txt"
    key.write_text("v3-nøgle")
    special = {"Go.": "Gå nu, gå nu, gå nu, gå nu nu!", "What's up?": "Hvad så, bitches?", "Uh.": ""}

    def translate(kwargs):
        lines = [{"i": x["i"], "da": special.get(x["en"], f"Dansk {x['i']}")} for x in request_payload(kwargs)["lines"]]
        return json.dumps({"lines": lines}, ensure_ascii=False)

    fake = SmartClaude(translate=translate)
    out = tmp_path / "ud.srt"
    rc = run_translate_test(
        cfg(tmdb_api_key_file=str(key)),
        en,
        str(out),
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
    assert "Kontakter: prompt v4, kontekst on, ordliste on, parallel 4, prompt-cache on" in report
    assert "Rettelsesflag fra kontrollen: 1 (english_word=1)" in report
    assert "Informationsflag (sendes ikke til rettelse): 1 (reading_speed=1)" in report
    assert "heraf 1 med kun fyldlyde fjernet" in report
    assert "cache-læs 0, cache-skriv 0" in report
    assert len(fake.of("oversæt")) == 3  # ingen genforsøg for "Uh."
    for req in fake.of("oversæt"):
        assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
