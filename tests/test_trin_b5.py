"""Trin B5: --batch-api i oversaet-test (Anthropic Message Batches) med falske svar - ingen rigtige kald."""

import json
import logging
from types import SimpleNamespace

import pytest

from oversaetter.__main__ import main
from oversaetter.config import BATCH_FACTOR, model_price
from oversaetter.oversaet import BatchApi, run_translate_test, translate_all
from tests.helpers import cfg, srt
from tests.test_oversaet import FakeProvider, english, request_payload, translations, write_en


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeBatches:
    """Falsk client.messages.batches. answer(params) giver svarteksten, "errored" eller None (mangler i svarene).

    ready_after: sekunder (efter clock) før batchen er "ended"; None = bliver aldrig færdig (kun ved aflysning).
    """

    def __init__(self, answer, clock, ready_after=60.0, inp=1000, out=200, cache_read=0, cache_write=0):
        self.answer, self.clock, self.ready_after = answer, clock, ready_after
        self.usage = SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )
        self.requests: list[dict] = []
        self.created = None
        self.retrieves = 0
        self.canceled = False

    def _batch(self):
        done = self.canceled or (self.ready_after is not None and self.clock() >= self.created + self.ready_after)
        return SimpleNamespace(
            id="msgbatch_1",
            processing_status="ended" if done else "in_progress",
            request_counts=SimpleNamespace(processing=0 if done else len(self.requests), succeeded=0, errored=0),
        )

    def create(self, requests):
        self.requests = list(requests)
        self.created = self.clock()
        return self._batch()

    def retrieve(self, batch_id):
        self.retrieves += 1
        return self._batch()

    def cancel(self, batch_id):
        self.canceled = True
        return SimpleNamespace(id=batch_id, processing_status="canceling")

    def results(self, batch_id):
        for r in self.requests:
            if self.canceled and self.ready_after is None:
                result = SimpleNamespace(type="canceled")
            else:
                text = self.answer(r["params"])
                if text is None:
                    continue
                if text == "errored":
                    result = SimpleNamespace(
                        type="errored", error=SimpleNamespace(error=SimpleNamespace(type="api_error"))
                    )
                else:
                    message = SimpleNamespace(
                        content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=self.usage
                    )
                    result = SimpleNamespace(type="succeeded", message=message)
            yield SimpleNamespace(custom_id=r["custom_id"], result=result)


def first_line(params) -> int:
    return request_payload(params)["lines"][0]["i"]


def make(answer, ready_after=60.0, **kw):
    clock = FakeClock()
    fake = FakeProvider("anthropic", translations)  # direkte kald: bid 1 og dem, der oversættes bagefter
    fake.batches = FakeBatches(answer, clock, ready_after, **kw)
    return fake, clock


def run_batch(fake, clock, n=200, timeout=90):
    api = BatchApi(fake, timeout, sleep=clock.sleep, clock=clock)
    return translate_all(english(n), "anthropic", fake, "claude-opus-5-5", None, 50, 60.0, batch_api=api, cache=True)


def write_long(tmp_path, n):
    """n replikker, 3 sekunder fra hinanden (write_en kan kun lave under et minut)."""

    def ts(sec: int) -> str:
        return f"00:{sec // 60:02d}:{sec % 60:02d},000"

    path = tmp_path / "film.en.srt"
    path.write_text(srt(*[(ts(10 + i * 3), ts(11 + i * 3), f"Line {i + 1}") for i in range(n)]))
    return str(path)


def direct_firsts(fake) -> list[int]:
    return [request_payload(r)["lines"][0]["i"] for r in fake.requests]


# --- flowet ------------------------------------------------------------------------------------------
def test_all_succeed_in_batch():
    fake, clock = make(translations)
    res = run_batch(fake, clock)
    assert direct_firsts(fake) == [1]  # kun bid 1 direkte
    assert [r["custom_id"] for r in fake.batches.requests] == ["2", "3", "4"]
    assert res.da == [f"Dansk {i + 1}" for i in range(200)] and res.fallback == []
    b = res.batch
    assert (b.sent, b.ok, b.direct_after, b.canceled) == (3, 3, 0, False)
    assert b.seconds == 60 and b.input_tokens == 3000 and b.output_tokens == 600
    assert res.input_tokens == 1000  # direkte tokens tælles for sig
    assert fake.batches.retrieves == 2  # spurgt hvert 30. sekund


def test_batch_requests_match_direct_calls():
    fake, clock = make(translations)
    run_batch(fake, clock)
    direct = fake.requests[0]
    params = fake.batches.requests[0]["params"]
    assert set(params) == set(direct)
    for key in ("model", "max_tokens", "system", "output_config"):
        assert params[key] == direct[key]
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["output_config"]["format"]["type"] == "json_schema"
    # context_before/glossary kun fra det, der fandtes ved afsendelsen (bid 1)
    bid2 = request_payload(params)
    assert [x["i"] for x in bid2["context_before"]] == [46, 47, 48, 49, 50]
    assert bid2["context_before"][0]["da"] == "Dansk 46"
    assert request_payload(fake.batches.requests[1]["params"])["context_before"] == []


def test_errored_request_is_translated_directly(caplog):
    fake, clock = make(lambda p: "errored" if first_line(p) == 51 else translations(p))
    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        res = run_batch(fake, clock)
    assert direct_firsts(fake) == [1, 51]
    assert res.da == [f"Dansk {i + 1}" for i in range(200)] and res.fallback == []
    assert (res.batch.ok, res.batch.direct_after) == (2, 1)
    assert "errored i batchen (api_error) - oversættes direkte" in caplog.text
    # direkte bagefter får den nu færdige danske kontekst
    assert request_payload(fake.requests[1])["context_before"][-1]["da"] == "Dansk 50"


def test_missing_result_is_translated_directly(caplog):
    fake, clock = make(lambda p: None if first_line(p) == 101 else translations(p))
    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        res = run_batch(fake, clock)
    assert direct_firsts(fake) == [1, 101]
    assert res.fallback == [] and (res.batch.ok, res.batch.direct_after) == (2, 1)
    assert "manglede i batchens svar" in caplog.text


def test_invalid_json_in_batch_goes_direct(caplog):
    fake, clock = make(lambda p: "ikke json" if first_line(p) == 151 else translations(p))
    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        res = run_batch(fake, clock)
    assert direct_firsts(fake) == [1, 151]
    assert res.da[150:] == [f"Dansk {i + 1}" for i in range(150, 200)]
    assert "ugyldig JSON" in caplog.text
    assert res.batch.input_tokens == 3000  # også det ugyldige svar er betalt og tælles


def test_missing_numbers_in_batch_go_direct():
    fake, clock = make(lambda p: translations(p, skip={60}) if first_line(p) == 51 else translations(p))
    res = run_batch(fake, clock)
    assert direct_firsts(fake) == [1, 51] and res.da[59] == "Dansk 60" and res.fallback == []


def test_direct_after_batch_uses_existing_retry_logic():
    fake, clock = make(lambda p: "errored" if first_line(p) == 51 else translations(p))
    fake.answer = lambda kw: (
        translations(kw, skip={60}) if request_payload(kw)["lines"][0]["i"] == 51 else (translations(kw))
    )
    res = run_batch(fake, clock)
    # bid 51-100 direkte: mangler 60 to gange, så delt i to (51-75 og 76-100)
    assert direct_firsts(fake) == [1, 51, 51, 51, 76]
    assert res.fallback == [59]


def test_timeout_cancels_and_translates_directly(caplog):
    fake, clock = make(translations, ready_after=None)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = run_batch(fake, clock, timeout=12)
    assert fake.batches.canceled
    assert direct_firsts(fake) == [1, 51, 101, 151]
    assert res.da == [f"Dansk {i + 1}" for i in range(200)] and res.fallback == []
    assert res.batch.canceled and (res.batch.ok, res.batch.direct_after) == (0, 3)
    assert res.batch.seconds >= 12 * 60
    assert "aflyser den" in caplog.text
    assert caplog.text.count("Venter stadig på batch") == 2  # hvert 5. minut: efter 5 og 10 min


def test_batch_that_cannot_be_sent_goes_direct(caplog):
    fake, clock = make(translations)

    def boom(requests):
        raise RuntimeError("nede")

    fake.batches.create = boom
    with caplog.at_level(logging.ERROR, logger="oversaetter"):
        res = run_batch(fake, clock)
    assert direct_firsts(fake) == [1, 51, 101, 151] and res.fallback == []
    assert "Batchen kunne ikke sendes" in caplog.text


def test_single_batch_sends_no_batch():
    fake, clock = make(translations)
    res = run_batch(fake, clock, n=30)
    assert fake.batches.requests == [] and res.batch.sent == 0 and res.da[0] == "Dansk 1"


# --- pris ----------------------------------------------------------------------------------------------
def test_batch_factor_halves_every_token_kind():
    full = model_price("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    half = model_price("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000, factor=BATCH_FACTOR)
    # 4 ind + 20 ud + 0,4 cache-læs + 5 cache-skriv = 29,4; halv pris 14,7
    assert abs(full - 29.4) < 1e-9 and abs(half - 14.7) < 1e-9
    assert model_price("ukendt", 1, 1, factor=0.5) is None


def test_report_splits_price(tmp_path, capsys):
    en = write_long(tmp_path, 120)  # 3 bidder: 1 direkte, 2 i batchen
    out = str(tmp_path / "ud.srt")
    fake, clock = make(translations, inp=1_000_000, out=100_000, cache_read=500_000, cache_write=200_000)
    fake.inp, fake.out = 1_000_000, 100_000
    c = cfg(prompt_cache=True)
    code = run_translate_test(
        c, en, out, "anthropic", "claude-opus-5-5", client=fake, clock=clock, sleep=clock.sleep, batch_api=True
    )
    assert code == 0
    report = capsys.readouterr().out
    assert "Batch: 2 bidder sendt, 2 lykkedes, 0 oversat direkte bagefter, ventetid 1 min 0 s" in report
    # batch: 2 * (1.000.000*4 + 100.000*20 + 500.000*0,4 + 200.000*5) / 1e6 = 2 * 7,2 = 14,4 -> halv pris 7,2
    assert "Pris batch (50 %): 7.2000 USD (tokens ind 3400000, ud 200000)" in report
    # direkte: 1.000.000*4 + 100.000*20 = 6
    assert "Pris direkte: 6.0000 USD" in report
    assert "Pris: 13.2000 USD" in report
    assert "Tokens ind: 4400000 (heraf cache-læs 1000000, cache-skriv 400000)" in report
    assert "Tokens ud: 300000" in report
    assert "batch-api on (timeout 90 min)" in report


def test_report_without_batch_api_is_unchanged(tmp_path, capsys):
    en = write_en(tmp_path, 3)
    fake = FakeProvider("anthropic", translations)
    assert run_translate_test(cfg(), en, str(tmp_path / "ud.srt"), "anthropic", "claude-opus-5-5", client=fake) == 0
    report = capsys.readouterr().out
    assert "Batch:" not in report and "batch-api" not in report


# --- afvisning og kommandolinje -------------------------------------------------------------------------
@pytest.mark.parametrize("provider", ["deepseek", "gemini"])
def test_batch_api_rejected_for_other_providers(tmp_path, caplog, provider):
    en = write_en(tmp_path)
    fake = FakeProvider(provider, translations)
    with caplog.at_level(logging.ERROR, logger="oversaetter"):
        code = run_translate_test(cfg(), en, str(tmp_path / "ud.srt"), provider, "model-x", client=fake, batch_api=True)
    assert code == 2 and fake.requests == []
    assert "--batch-api virker kun med --provider anthropic" in caplog.text
    assert not (tmp_path / "ud.srt").exists()


def test_cli_passes_batch_switches(tmp_path, monkeypatch):
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr("oversaetter.__main__.run_translate_test", fake_run)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = ["oversaet-test", "--en", "a.srt", "--out", "b.srt", "--provider", "anthropic", "--model", "m"]
    assert main(base) == 0
    assert seen["batch_api"] is False and seen["batch_timeout"] == 90
    assert main([*base, "--batch-api", "--batch-timeout", "45"]) == 0
    assert seen["batch_api"] is True and seen["batch_timeout"] == 45


def test_cli_rejects_deepseek_with_batch_api(tmp_path, monkeypatch, capsys):
    en = write_en(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    args = ["oversaet-test", "--en", en, "--out", str(tmp_path / "ud.srt"), "--provider", "deepseek"]
    assert main([*args, "--model", "deepseek-v4-pro", "--batch-api"]) == 2  # main logger til stdout
    assert "--batch-api virker kun med --provider anthropic" in capsys.readouterr().out


def test_batch_json_dump_is_plain_dicts():
    fake, clock = make(translations)
    run_batch(fake, clock)
    json.dumps(fake.batches.requests)  # anmodningerne kan sendes som de er (rene dicts)


# --- rettelse: venteløkken og --batch-id ----------------------------------------------------------------
class SequenceBatches(FakeBatches):
    """retrieve giver statusserne i rækkefølge (et Exception-objekt kastes); den sidste gentages."""

    def __init__(self, answer, clock, statuses):
        super().__init__(answer, clock, ready_after=None)
        self.statuses = list(statuses)
        self.ids: list[str] = []

    def retrieve(self, batch_id):
        self.ids.append(batch_id)
        self.retrieves += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        done = status == "ended"
        return SimpleNamespace(
            id=batch_id,
            processing_status=status,
            request_counts=SimpleNamespace(
                processing=0 if done else 3, succeeded=3 if done else 0, errored=0, canceled=0, expired=0
            ),
        )

    def results(self, batch_id):
        self.canceled = False
        return super().results(batch_id)


def make_seq(statuses, answer=translations):
    clock = FakeClock()
    fake = FakeProvider("anthropic", translations)
    fake.batches = SequenceBatches(answer, clock, statuses)
    return fake, clock


def earlier_batch(n: int, tmp_path=None) -> list[dict]:
    """Anmodningerne fra en tidligere kørsel med --batch-api (den batch, der allerede er betalt)."""
    fake, clock = make(translations)
    if tmp_path is None:
        run_batch(fake, clock, n=n)
    else:
        run_translate_test(
            cfg(),
            write_long(tmp_path, n),
            str(tmp_path / "foer.srt"),
            "anthropic",
            "claude-opus-5-5",
            client=fake,
            clock=clock,
            sleep=clock.sleep,
            batch_api=True,
        )
        (tmp_path / "foer.srt").unlink()
    return fake.batches.requests


def test_loop_sees_in_progress_twice_then_ended(caplog):
    fake, clock = make_seq(["in_progress", "in_progress", "ended"])
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = run_batch(fake, clock)
    assert fake.batches.retrieves == 3 and clock.now == 90  # spurgt hvert 30. sekund
    assert res.batch.ok == 3 and res.fallback == [] and direct_firsts(fake) == [1]
    assert res.da == [f"Dansk {i + 1}" for i in range(200)]
    asks = [r for r in caplog.records if r.getMessage().startswith("Batch msgbatch_1: status")]
    assert [r.levelno for r in asks] == [logging.INFO] * 3  # hvert spørgsmål logges på INFO
    assert "status in_progress (i gang 3, lykkedes 0, fejlet 0" in asks[0].getMessage()
    assert "status ended (i gang 0, lykkedes 3, fejlet 0" in asks[2].getMessage()
    assert "er færdig efter 1 min 30 s - henter svarene" in caplog.text


def test_retrieve_that_raises_once_does_not_stop_the_loop(caplog):
    fake, clock = make_seq(["in_progress", RuntimeError("forbindelsen døde"), "ended"])
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = run_batch(fake, clock)
    assert fake.batches.retrieves == 3 and res.batch.ok == 3 and res.fallback == []
    assert "Kunne ikke spørge til batch msgbatch_1 (RuntimeError: forbindelsen døde) - prøver igen" in caplog.text


def test_hanging_retrieve_is_abandoned_and_retried(caplog):
    """Årsagen til fejlen: et opslag, der hænger, må ikke få løkken til at stå stille."""
    import threading

    release = threading.Event()
    fake, clock = make_seq(["in_progress", "ended"])
    seq_retrieve = fake.batches.retrieve
    calls = {"n": 0}

    def retrieve(batch_id):
        calls["n"] += 1
        if calls["n"] == 1:
            release.wait(5)  # hænger (som en død forbindelse)
        return seq_retrieve(batch_id)

    fake.batches.retrieve = retrieve
    api = BatchApi(fake, 90, sleep=clock.sleep, clock=clock, call_timeout=0.05)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = translate_all(english(200), "anthropic", fake, "claude-opus-5-5", None, 50, 60.0, batch_api=api)
    release.set()
    assert res.batch.ok == 3 and res.fallback == []
    assert "Kunne ikke spørge til batch msgbatch_1 (TimeoutError" in caplog.text


def test_polling_uses_short_timeout_without_sdk_retries():
    fake, clock = make_seq(["ended"])
    seen = {}

    def with_options(**kw):
        seen.update(kw)
        return fake

    fake.with_options = with_options
    BatchApi(fake, 90, call_timeout=60)
    assert seen == {"timeout": 60, "max_retries": 0}


def test_batch_id_fetches_results_and_translates_missing_directly(caplog):
    fake, clock = make_seq(["ended"], answer=lambda p: None if first_line(p) == 101 else translations(p))
    fake.batches.requests = earlier_batch(200)
    api = BatchApi(fake, 90, sleep=clock.sleep, clock=clock, batch_id="msgbatch_gammel")
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = translate_all(english(200), "anthropic", fake, "claude-opus-5-5", None, 50, 60.0, batch_api=api)
    assert fake.batches.created is None  # intet sendt
    assert fake.batches.ids == ["msgbatch_gammel"] and clock.now == 0  # spurgt med det samme
    assert direct_firsts(fake) == [1, 101]  # bid 1 og den manglende bid direkte
    assert res.da == [f"Dansk {i + 1}" for i in range(200)] and res.fallback == []
    assert res.batch.reused and (res.batch.sent, res.batch.ok, res.batch.direct_after) == (3, 2, 1)
    assert res.calls == 2  # kun de direkte kald
    assert "Henter svarene fra den eksisterende batch msgbatch_gammel" in caplog.text


def test_batch_id_report(tmp_path, capsys):
    earlier = earlier_batch(120, tmp_path)
    capsys.readouterr()
    en = write_long(tmp_path, 120)
    fake, clock = make_seq(["ended"])
    fake.batches.requests = earlier
    code = run_translate_test(
        cfg(),
        en,
        str(tmp_path / "ud.srt"),
        "anthropic",
        "claude-opus-5-5",
        client=fake,
        clock=clock,
        sleep=clock.sleep,
        batch_id="msgbatch_test27",
    )
    assert code == 0 and fake.batches.created is None
    report = capsys.readouterr().out
    assert "Batch: 2 bidder hentet fra msgbatch_test27, 2 lykkedes, 0 oversat direkte bagefter" in report
    assert "allerede betalt i den hentede batch" in report and "batch-id msgbatch_test27" in report


def test_batch_id_and_batch_api_together_are_rejected(tmp_path, caplog):
    en = write_en(tmp_path)
    fake, clock = make_seq(["ended"])
    with caplog.at_level(logging.ERROR, logger="oversaetter"):
        code = run_translate_test(
            cfg(),
            en,
            str(tmp_path / "ud.srt"),
            "anthropic",
            "claude-opus-5-5",
            client=fake,
            batch_api=True,
            batch_id="msgbatch_1",
        )
    assert code == 2 and fake.requests == [] and not (tmp_path / "ud.srt").exists()
    assert "ikke begge" in caplog.text


def test_batch_id_rejected_for_deepseek(tmp_path, caplog):
    en = write_en(tmp_path)
    fake = FakeProvider("deepseek", translations)
    with caplog.at_level(logging.ERROR, logger="oversaetter"):
        code = run_translate_test(cfg(), en, str(tmp_path / "ud.srt"), "deepseek", "m", client=fake, batch_id="x")
    assert code == 2 and "--batch-id virker kun med --provider anthropic" in caplog.text


def test_cli_passes_batch_id(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr("oversaetter.__main__.run_translate_test", lambda *a, **kw: seen.update(kw) or 0)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = ["oversaet-test", "--en", "a.srt", "--out", "b.srt", "--provider", "anthropic", "--model", "m"]
    assert main(base) == 0 and seen["batch_id"] is None
    assert main([*base, "--batch-id", "msgbatch_01MjM5d2aartaJohvrK9TZyB"]) == 0
    assert seen["batch_id"] == "msgbatch_01MjM5d2aartaJohvrK9TZyB"
