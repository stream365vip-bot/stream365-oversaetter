"""Testtilstanden oversaet-test med falske svar fra alle tre providers. Alle tekster er selvskrevne."""

import json
import logging
import os
import threading
import time
from types import SimpleNamespace

import httpx2
import openai
import pytest

from oversaetter import oversaet
from oversaetter.__main__ import main
from oversaetter.config import Config, model_price
from oversaetter.oversaet import SYSTEM_PROMPT, run_translate_test, translate_all
from oversaetter.postprocess import make_client as drift_client
from tests.helpers import cfg, srt


def request_payload(kwargs) -> dict:
    """Det JSON, der blev sendt til modellen (samme for alle providers)."""
    if "messages" in kwargs and kwargs["messages"][0]["role"] == "system":
        content = kwargs["messages"][1]["content"]
    else:
        content = kwargs["messages"][0]["content"]
    return json.loads(content.split("\n\n", 1)[1])


def translations(kwargs, skip=()) -> str:
    lines = [{"i": x["i"], "da": f"Dansk {x['i']}"} for x in request_payload(kwargs)["lines"] if x["i"] not in skip]
    return json.dumps({"lines": lines}, ensure_ascii=False)


class FakeProvider:
    """Falsk klient for anthropic (messages.create) eller deepseek/gemini (chat.completions.create).

    answer(kwargs) giver svarteksten, en undtagelse, eller ("stop", tekst) for et afbrudt svar.
    """

    def __init__(self, provider: str, answer, inp=1000, out=200):
        self.provider = provider
        self.answer = answer
        self.inp, self.out = inp, out
        self.requests: list[dict] = []
        self.messages = self
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.answer(kwargs)
        if isinstance(reply, Exception):
            raise reply
        stop, text = reply if isinstance(reply, tuple) else (None, reply)
        if self.provider == "anthropic":
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason=stop or "end_turn",
                usage=SimpleNamespace(
                    input_tokens=self.inp,
                    output_tokens=self.out,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=stop or "stop")],
            usage=SimpleNamespace(prompt_tokens=self.inp, completion_tokens=self.out, total_tokens=self.inp + self.out),
        )

    def payload(self, n=-1) -> dict:
        return request_payload(self.requests[n])


PROVIDERS = ["anthropic", "deepseek", "gemini"]


def english(n):
    return [f"we talk about thing {i + 1}." for i in range(n)]


def run(provider, answer, n=10, batch=50, effort=None, timeout=60.0):
    fake = FakeProvider(provider, answer)
    res = translate_all(english(n), provider, fake, "model-x", effort, batch, timeout)
    return fake, res


# --- komplet svar ------------------------------------------------------------------------------------
@pytest.mark.parametrize("provider", PROVIDERS)
def test_complete_answer_in_batches(provider):
    fake, res = run(provider, translations, n=120, batch=50, effort="high")
    assert len(fake.requests) == 3 and res.batches == 3 and res.calls == 3
    assert res.da == [f"Dansk {i + 1}" for i in range(120)]
    assert res.fallback == []
    assert [x["i"] for x in fake.payload(0)["lines"]] == list(range(1, 51))
    assert [x["i"] for x in fake.payload(2)["lines"]] == list(range(101, 121))
    assert res.input_tokens == 3000 and res.output_tokens == 600
    req = fake.requests[0]
    if provider == "anthropic":
        assert req["system"] == SYSTEM_PROMPT
        assert req["output_config"]["effort"] == "high"
        assert req["output_config"]["format"]["type"] == "json_schema"
    else:
        assert req["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
        assert req["response_format"] == {"type": "json_object"}
        assert req["temperature"] == 0.3
        assert ("reasoning_effort" in req) == (provider == "gemini")


def test_line_breaks_are_kept():
    def answer(kwargs):
        lines = [{"i": x["i"], "da": "Første linje\\nAnden linje"} for x in request_payload(kwargs)["lines"]]
        return json.dumps({"lines": lines})

    _, res = run("deepseek", answer, n=2)
    assert res.da == ["Første linje\nAnden linje"] * 2


def test_json_in_code_fence_is_accepted():
    _, res = run("gemini", lambda kw: "```json\n" + translations(kw) + "\n```", n=3)
    assert res.da == ["Dansk 1", "Dansk 2", "Dansk 3"]


# --- manglende numre: igen, halvering, engelsk ------------------------------------------------------------
@pytest.mark.parametrize("provider", PROVIDERS)
def test_missing_number_is_retried_once(provider, caplog):
    calls = []

    def answer(kwargs):
        calls.append(1)
        return translations(kwargs, skip={7} if len(calls) == 1 else ())

    with caplog.at_level(logging.INFO, logger="oversaetter"):
        fake, res = run(provider, answer, n=10)
    assert len(fake.requests) == 2 and res.fallback == []
    assert res.da[6] == "Dansk 7"
    assert "mangler 7" in caplog.text and "prøver igen" in caplog.text


@pytest.mark.parametrize("provider", PROVIDERS)
def test_missing_twice_is_halved(provider):
    def answer(kwargs):
        nums = [x["i"] for x in request_payload(kwargs)["lines"]]
        return translations(kwargs, skip={3} if len(nums) == 50 else ())

    fake, res = run(provider, answer, n=50)
    assert [len(fake.payload(k)["lines"]) for k in range(4)] == [50, 50, 25, 25]
    assert [x["i"] for x in fake.payload(3)["lines"]] == list(range(26, 51))
    assert res.fallback == [] and res.da[2] == "Dansk 3"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_still_missing_after_halving_keeps_english(provider, caplog):
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        fake, res = run(provider, lambda kw: translations(kw, skip={3, 40}), n=50)
    assert len(fake.requests) == 4
    assert res.fallback == [2, 39]
    assert res.da[2] == "we talk about thing 3." and res.da[39] == "we talk about thing 40."
    assert res.da[3] == "Dansk 4"  # de gyldige i samme halvdel beholdes
    assert "Replik 3 beholder den engelske tekst" in caplog.text
    assert "Replik 40 beholder den engelske tekst" in caplog.text


def test_extra_and_empty_numbers_count_as_errors():
    calls = []

    def answer(kwargs):
        calls.append(1)
        if len(calls) == 1:
            return json.dumps({"lines": [{"i": 1, "da": "Et"}, {"i": 2, "da": " "}, {"i": 99, "da": "Ekstra"}]})
        return translations(kwargs)

    fake, res = run("anthropic", answer, n=2)
    assert len(fake.requests) == 2 and res.da == ["Dansk 1", "Dansk 2"]


def test_single_cue_is_not_halved():
    fake, res = run("deepseek", lambda kw: '{"lines": []}', n=1)
    assert len(fake.requests) == 2 and res.fallback == [0]


# --- ugyldig JSON, afbrudt svar og timeout -------------------------------------------------------------
@pytest.mark.parametrize("provider", PROVIDERS)
def test_invalid_json_is_retried(provider, caplog):
    calls = []

    def answer(kwargs):
        calls.append(1)
        return "Her er oversættelsen: {ikke json" if len(calls) == 1 else translations(kwargs)

    with caplog.at_level(logging.INFO, logger="oversaetter"):
        fake, res = run(provider, answer, n=5)
    assert len(fake.requests) == 2 and res.fallback == []
    assert "ugyldig JSON" in caplog.text


@pytest.mark.parametrize("provider", PROVIDERS)
def test_always_invalid_json_falls_back_to_english(provider):
    fake, res = run(provider, lambda kw: "slet ikke json", n=4)
    assert len(fake.requests) == 4 and res.fallback == [0, 1, 2, 3]
    assert res.da == english(4)
    assert res.input_tokens == 4000  # betalte tokens tælles stadig


@pytest.mark.parametrize("provider", PROVIDERS)
def test_cut_off_answer_is_an_error(provider):
    stop = "max_tokens" if provider == "anthropic" else "length"
    calls = []

    def answer(kwargs):
        calls.append(1)
        return (stop, translations(kwargs)) if len(calls) == 1 else translations(kwargs)

    fake, res = run(provider, answer, n=3)
    assert len(fake.requests) == 2 and res.fallback == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_timeout_from_sdk_is_retried(provider):
    request = httpx2.Request("POST", "https://example.invalid/")
    calls = []

    def answer(kwargs):
        calls.append(1)
        return openai.APITimeoutError(request=request) if len(calls) == 1 else translations(kwargs)

    fake, res = run(provider, answer, n=3)
    assert len(fake.requests) == 2 and res.fallback == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_call_that_never_answers_is_abandoned(provider, caplog):
    release = threading.Event()

    def answer(kwargs):
        if len(request_payload(kwargs)["lines"]) == 4 and not release.is_set():
            release.wait(30)  # hænger
        return translations(kwargs)

    started = time.monotonic()
    try:
        with caplog.at_level(logging.INFO, logger="oversaetter"):
            fake, res = run(provider, answer, n=4, timeout=0.05)
    finally:
        release.set()
    assert time.monotonic() - started < 10
    # to forsøg med hele bidden hænger; de to halvdele (2+2) svarer
    assert len(fake.requests) == 4 and res.fallback == [] and res.da == ["Dansk 1", "Dansk 2", "Dansk 3", "Dansk 4"]
    assert "(timeout)" in caplog.text


# --- kontekst ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("provider", PROVIDERS)
def test_context_is_sent(provider):
    en = english(12)
    en[1] = "Hello, JD."
    en[3] = "Let's go."
    en[5] = "Let's go."
    fake = FakeProvider(provider, translations)
    translate_all(en, provider, fake, "model-x", None, 10, 60.0)
    first, second = fake.payload(0), fake.payload(1)
    assert first["context_before"] == [] and first["glossary"] == []
    assert [x["i"] for x in first["context_after"]] == [11, 12]
    assert [x["i"] for x in second["lines"]] == [11, 12]
    # de 5 foregående engelske replikker med deres færdige danske oversættelse
    assert second["context_before"] == [{"i": i, "en": en[i - 1], "da": f"Dansk {i}"} for i in range(6, 11)]
    assert second["context_after"] == []
    # navne og faste udtryk fra før konteksten
    assert second["glossary"] == [{"en": "Hello, JD.", "da": "Dansk 2"}, {"en": "Let's go.", "da": "Dansk 4"}]


def test_context_for_second_half_has_danish_from_first_half():
    def answer(kwargs):
        nums = [x["i"] for x in request_payload(kwargs)["lines"]]
        return translations(kwargs, skip={1} if len(nums) == 10 else ())

    fake, _ = run("deepseek", answer, n=10, batch=10)
    second_half = fake.payload(3)
    assert [x["i"] for x in second_half["lines"]] == [6, 7, 8, 9, 10]
    assert [x["da"] for x in second_half["context_before"]] == [f"Dansk {i}" for i in range(1, 6)]


def test_glossary_is_capped_at_30():
    en = [f"I met Robin{i} here." for i in range(80)]
    fake = FakeProvider("deepseek", translations)
    translate_all(en, "deepseek", fake, "model-x", None, 40, 60.0)
    glossary = fake.payload(1)["glossary"]
    assert len(glossary) == 30 and glossary[-1]["en"] == "I met Robin34 here."


def test_prompt_has_the_rules_and_json_format():
    for text in ("1. Write natural spoken Danish", '"beast" -> "uhyre"', "15. ", "at most 40 characters"):
        assert text in SYSTEM_PROMPT
    assert '{"lines":[{"i":<number>,"da":"<Danish text>"}]}' in SYSTEM_PROMPT


# --- rapport og fil ------------------------------------------------------------------------------------
def write_en(tmp_path, n=3):
    path = tmp_path / "film.en.srt"
    path.write_text(
        srt(*[(f"00:00:{10 + i * 3:02d},000", f"00:00:{11 + i * 3:02d},000", f"Line {i + 1}") for i in range(n)])
    )
    return str(path)


def test_report_price_and_file(tmp_path, capsys):
    en = write_en(tmp_path, 3)
    out = str(tmp_path / "film.da.srt")
    fake = FakeProvider("deepseek", translations, inp=1_000_000, out=500_000)
    c = cfg(credit_text="Oversat af Claude")
    assert run_translate_test(c, en, out, "deepseek", "deepseek-v4-pro", client=fake) == 0
    report = capsys.readouterr().out
    # 1.000.000 ind * 0,66 + 500.000 ud * 1,98 pr. million = 0,66 + 0,99
    assert "Pris: 1.6500 USD" in report
    assert "Provider: deepseek" in report and "Model: deepseek-v4-pro (effort -)" in report
    assert "Replikker: 3" in report and "Bidder: 1 (kald i alt: 1)" in report
    assert "Tokens ind: 1000000" in report and "Tokens ud: 500000" in report
    assert "Faldt tilbage til engelsk: 0" in report and "Tid: " in report
    text = open(out, encoding="utf-8").read()
    assert "Dansk 1" in text and "Oversat af" not in text and text.startswith("1\n00:00:10,000")
    assert sorted(os.listdir(tmp_path)) == ["film.da.srt", "film.en.srt"]  # ingen andre filer


def test_report_flags_and_fallback(tmp_path, capsys):
    en = write_en(tmp_path, 3)
    out = str(tmp_path / "ud.srt")
    fake = FakeProvider("anthropic", lambda kw: translations(kw, skip={2}))
    assert run_translate_test(cfg(), en, out, "anthropic", "claude-sonnet-5-5", effort="low", client=fake) == 0
    report = capsys.readouterr().out
    assert "Faldt tilbage til engelsk: 1 (replik 2)" in report
    assert "Rettelsesflag fra kontrollen:" in report and "Model: claude-sonnet-5-5 (effort low)" in report
    assert fake.requests[0]["output_config"]["effort"] == "low"


@pytest.mark.parametrize(
    "model,price",
    [
        ("claude-haiku-5-5", 0.10 + 0.50),
        ("claude-sonnet-5-5", 2 + 10),
        ("claude-opus-5-5", 4 + 20),
        ("deepseek-v4-pro", 0.66 + 1.98),
        ("deepseek-flash", 0.15 + 0.60),
        ("gemini-3.8-flash", 0.75 + 3.75),
    ],
)
def test_price_table(model, price):
    assert abs(model_price(model, 1_000_000, 1_000_000) - price) < 1e-9


def test_unknown_model_gives_price_unknown(tmp_path, capsys):
    en = write_en(tmp_path)
    fake = FakeProvider("gemini", translations)
    assert model_price("ukendt-model", 10, 10) is None
    assert run_translate_test(cfg(), en, str(tmp_path / "ud.srt"), "gemini", "ukendt-model", client=fake) == 0
    assert "Pris: pris ukendt" in capsys.readouterr().out


def test_existing_out_is_not_overwritten(tmp_path, caplog):
    en = write_en(tmp_path)
    out = tmp_path / "ud.srt"
    out.write_text("min fil")
    fake = FakeProvider("deepseek", translations)
    assert run_translate_test(cfg(), en, str(out), "deepseek", "deepseek-flash", client=fake) == 2
    assert run_translate_test(cfg(), en, en, "deepseek", "deepseek-flash", client=fake) == 2
    assert out.read_text() == "min fil" and fake.requests == []


# --- nøglefiler ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("provider,name", [("anthropic", "Anthropic"), ("deepseek", "DeepSeek"), ("gemini", "Gemini")])
def test_missing_key_gives_danish_error(tmp_path, caplog, provider, name):
    en = write_en(tmp_path)
    out = tmp_path / "ud.srt"
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert run_translate_test(cfg(), en, str(out), provider, "model-x") == 2
    assert f"Nøglen til {name} mangler" in caplog.text and "Driften er ikke påvirket" in caplog.text
    assert not out.exists()


def test_key_path_that_is_a_directory_is_explained(tmp_path, caplog):
    en = write_en(tmp_path)
    folder = tmp_path / "deepseek_api_key"
    folder.mkdir()
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        assert (
            run_translate_test(cfg(deepseek_api_key_file=str(folder)), en, str(tmp_path / "u.srt"), "deepseek", "m")
            == 2
        )
    assert "er en mappe" in caplog.text


def test_keys_found_give_real_clients(tmp_path):
    for name in ("deepseek", "gemini"):
        (tmp_path / name).write_text("falsk-nøgle\n")
    c = cfg(deepseek_api_key_file=str(tmp_path / "deepseek"), gemini_api_key_file=str(tmp_path / "gemini"))
    assert str(oversaet.make_client("deepseek", c).base_url).startswith("https://api.deepseek.com")
    assert "generativelanguage.googleapis.com/v1beta/openai" in str(oversaet.make_client("gemini", c).base_url)


def test_missing_test_keys_do_not_stop_drift(tmp_path):
    key = tmp_path / "anthropic"
    key.write_text("falsk-nøgle")
    c = Config.from_env({"ANTHROPIC_API_KEY_FILE": str(key), "DEEPSEEK_API_KEY_FILE": "/findes/ikke"})
    assert c.deepseek_api_key_file == "/findes/ikke" and c.gemini_api_key_file == "/run/secrets/gemini_api_key"
    assert drift_client(c) is not None  # korrekturen får stadig sin Anthropic-klient


def test_cli_without_keys_gives_danish_error(tmp_path, monkeypatch, caplog):
    en = write_en(tmp_path)
    out = tmp_path / "ud.srt"
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(tmp_path / "ingen"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    argv = ["oversaet-test", "--en", en, "--out", str(out), "--provider", "deepseek", "--model", "deepseek-flash"]
    assert main(argv) == 2
    assert not out.exists() and not (tmp_path / "data").exists()


def test_compose_mounts_keys_read_only():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    compose = open(os.path.join(root, "compose.yaml"), encoding="utf-8").read()
    assert "/opt/oversaetter/secrets/deepseek_api_key.txt:/run/secrets/deepseek_api_key:ro" in compose
    assert "/opt/oversaetter/secrets/gemini_api_key.txt:/run/secrets/gemini_api_key:ro" in compose
