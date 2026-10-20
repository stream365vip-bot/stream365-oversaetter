"""Trin B6: BATCH_API=true i driften (Anthropic Message Batches) med falske svar - ingen rigtige kald."""

import logging
import os
import sqlite3
from types import SimpleNamespace

import pytest

from oversaetter.config import BATCH_FACTOR, Config, model_price
from oversaetter.db import Database
from tests.fakes import FakeArr, en_sub
from tests.helpers import srt
from tests.test_direct import Clock, FakeAnthropic, da_text, payload, runner
from tests.test_trin_b5 import FakeBatches

LINES = 120  # 3 bidder af 50: bid 1 direkte, bid 2 og 3 i batchen
DIRECT = model_price("claude-opus-5-5", 1000, 200)  # ét direkte kald i FakeAnthropic
BATCHED = model_price("claude-opus-5-5", 1000, 200, factor=BATCH_FACTOR)  # ét svar fra batchen


def danish(params) -> str:
    """Svarteksten for en bid i batchen - den samme som et direkte kald ville give."""
    return FakeAnthropic.danish(params).content[0].text


def first_line(params) -> int:
    return payload(params)["lines"][0]["i"]


def ts(sec: int) -> str:
    return f"00:{sec // 60:02d}:{sec % 60:02d},000"


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def long_movie(arr, n=LINES):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(srt(*[(ts(10 + i * 3), ts(11 + i * 3), f"We talk about thing {i + 1}.") for i in range(n)]))
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    return video


class DbBatches(FakeBatches):
    """FakeBatches, der også husker, hvad databasen havde gemt som batch_id under ventetiden."""

    def __init__(self, answer, clock, db=None, ready_after=60.0, **kw):
        super().__init__(answer, clock, ready_after, **kw)
        self.db = db
        self.seen_ids: list[str | None] = []
        self.creates = 0

    def create(self, requests):
        self.creates += 1
        return super().create(requests)

    def retrieve(self, batch_id):
        if self.db is not None:
            self.seen_ids.append(self.db.get("movie:1")["batch_id"])
        return super().retrieve(batch_id)


def setup(arr, tmp_path, answer=danish, ready_after=60.0, **kw):
    clock = Clock()
    claude = FakeAnthropic()
    kw.setdefault("batch_api", True)
    r, db = runner(arr, tmp_path, claude=claude, clock=clock, **kw)
    claude.batches = DbBatches(answer, clock, db, ready_after)
    return r, db, claude


def direct_firsts(claude) -> list[int]:
    return [
        payload(r)["lines"][0]["i"]
        for r, kind in zip(claude.requests, claude.kinds(), strict=True)
        if kind == "oversæt"
    ]


def done_line(caplog) -> str:
    return next(rec.message for rec in caplog.records if rec.message.startswith("Færdig:"))


# --- hele flowet i driften -------------------------------------------------------------------------
def test_full_batch_run_uploads_and_prices_at_half(arr, tmp_path, caplog):
    video = long_movie(arr)
    r, db, claude = setup(arr, tmp_path)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert direct_firsts(claude) == [1]  # kun bid 1 direkte
    assert [x["custom_id"] for x in claude.batches.requests] == ["2", "3"]
    params, direct = claude.batches.requests[0]["params"], claude.requests[0]
    for key in ("model", "max_tokens", "system", "output_config"):
        assert params[key] == direct[key]  # samme instruks, prompt-cache, skema, effort og max_tokens
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    # id'et er gemt, mens der ventes, og slettet, når svarene er hentet
    assert claude.batches.seen_ids and set(claude.batches.seen_ids) == {"msgbatch_1"}
    row = db.get("movie:1")
    assert row["batch_id"] is None and row["status"] == "faerdig"
    assert len(arr.uploads) == 1 and "Dansk 120" in da_text(video) and "thing" not in da_text(video)
    # pris: direkte kald til fuld pris, batchens svar til halv pris - i items.cost_usd og i månedsbudgettet
    expected = len(claude.requests) * DIRECT + 2 * BATCHED
    assert row["cost_usd"] == pytest.approx(expected) and db.month_spend() == pytest.approx(expected)
    line = done_line(caplog)
    assert f"pris: {expected:.4f} USD (batch {2 * BATCHED:.4f} USD, direkte {expected - 2 * BATCHED:.4f} USD)" in line
    assert "batch: 2 bidder, 2 lykkedes, 0 direkte, ventetid 1 min 0 s" in line


def test_errored_bid_is_translated_directly(arr, tmp_path, caplog):
    long_movie(arr)
    r, db, claude = setup(arr, tmp_path, answer=lambda p: "errored" if first_line(p) == 51 else danish(p))
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert direct_firsts(claude) == [1, 51]
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["batch_id"] is None and len(arr.uploads) == 1
    assert "errored i batchen (api_error) - oversættes direkte" in caplog.text
    assert "batch: 2 bidder, 1 lykkedes, 1 direkte" in done_line(caplog)
    assert row["cost_usd"] == pytest.approx(len(claude.requests) * DIRECT + BATCHED)


def test_timeout_cancels_and_translates_directly(arr, tmp_path, caplog):
    long_movie(arr)
    r, db, claude = setup(arr, tmp_path, ready_after=None, batch_timeout_minutes=5)
    with caplog.at_level(logging.DEBUG, logger="oversaetter"):
        r.run_once()
    assert claude.batches.canceled
    assert direct_firsts(claude) == [1, 51, 101]
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["batch_id"] is None and len(arr.uploads) == 1
    assert "aflyser den" in caplog.text
    assert row["cost_usd"] == pytest.approx(len(claude.requests) * DIRECT)  # aflyste bidder koster intet
    # hvert opslag på DEBUG, en INFO-linje med status og tællere hvert 2. minut (efter 2 og 4 min)
    asks = [rec for rec in caplog.records if rec.message.startswith("Batch msgbatch_1: status")]
    assert asks and {rec.levelno for rec in asks} == {logging.DEBUG}
    waits = [rec for rec in caplog.records if rec.message.startswith("Venter stadig på batch")]
    assert len(waits) == 2 and {rec.levelno for rec in waits} == {logging.INFO}
    assert "status in_progress (i gang 2, lykkedes 0, fejlet 0" in waits[0].message


def test_batch_wait_does_not_count_against_translate_timeout(arr, tmp_path):
    long_movie(arr)
    r, db, claude = setup(
        arr, tmp_path, answer=lambda p: "errored" if first_line(p) == 51 else danish(p), translate_timeout_minutes=1
    )
    claude.batches.ready_after = 30 * 60  # batchen tager 30 minutter, tidsgrænsen pr. emne er 1 minut
    r.run_once()
    assert direct_firsts(claude) == [1, 51]
    assert db.get("movie:1")["status"] == "faerdig"


# --- genstart midt i ventetiden ------------------------------------------------------------------------
class Killed(BaseException):
    """Containeren stoppes (SIGKILL) midt i ventetiden."""


def test_restart_during_wait_fetches_saved_batch_without_resending(arr, tmp_path, caplog):
    video = long_movie(arr)
    r, db, claude = setup(arr, tmp_path, ready_after=None)
    real_retrieve = claude.batches.retrieve

    def killed(batch_id):
        real_retrieve(batch_id)
        raise Killed

    claude.batches.retrieve = killed
    with pytest.raises(Killed):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "i_gang" and row["batch_id"] == "msgbatch_1" and arr.uploads == []
    sent = claude.batches.requests
    spent_before = db.month_spend()

    # Ny container: samme database, batchen er blevet færdig i mellemtiden
    r2, db2, claude2 = setup(arr, tmp_path)
    claude2.batches.requests = sent  # Anthropic har stadig batchen
    claude2.batches.created = 0.0
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r2.resume()
        assert db2.get("movie:1")["status"] == "forsoeg_igen"
        r2.run_once()
    assert claude2.batches.creates == 0  # intet sendt igen
    assert direct_firsts(claude2) == [1]  # ordlisten/bid 1 direkte som ved --batch-id
    row = db2.get("movie:1")
    assert row["status"] == "faerdig" and row["batch_id"] is None and row["attempts"] == 1
    assert len(arr.uploads) == 1 and "Dansk 120" in da_text(video)
    assert "har en gemt batch (msgbatch_1)" in caplog.text
    assert "Henter svarene fra den gemte batch msgbatch_1 - intet sendes igen" in caplog.text
    assert "batch: 2 bidder fra gemt batch, 2 lykkedes, 0 direkte" in done_line(caplog)
    # batchens pris bogføres, når svarene hentes (den blev ikke bogført før genstarten)
    assert db2.month_spend() - spent_before == pytest.approx(len(claude2.requests) * DIRECT + 2 * BATCHED)


class GoneBatches(DbBatches):
    """Den gemte batch "msgbatch_gammel" er ukendt (404) eller udløbet; nye batcher virker som normalt."""

    def __init__(self, *a, how="404", **kw):
        super().__init__(*a, **kw)
        self.how = how

    def retrieve(self, batch_id):
        if batch_id == "msgbatch_gammel":
            if self.how == "404":
                raise type("NotFoundError", (Exception,), {"status_code": 404})("batch not found")
            return SimpleNamespace(id=batch_id, processing_status="ended", request_counts=SimpleNamespace(expired=2))
        return super().retrieve(batch_id)

    def results(self, batch_id):
        if batch_id == "msgbatch_gammel":
            return iter([SimpleNamespace(custom_id=c, result=SimpleNamespace(type="expired")) for c in ("2", "3")])
        return super().results(batch_id)


@pytest.mark.parametrize("how", ["404", "expired"])
def test_unknown_or_expired_saved_batch_is_cleared_and_sent_again(arr, tmp_path, caplog, how):
    video = long_movie(arr)
    english = arr.movies[1]["subtitles"]
    clock = Clock()
    claude = FakeAnthropic()
    r, db = runner(arr, tmp_path, claude=claude, clock=clock, batch_api=True)
    claude.batches = GoneBatches(danish, clock, db, how=how)
    r.run_once()  # søgning og oversættelse - derefter lades der, som om en gemt batch venter
    arr.movies[1]["subtitles"] = english
    os.remove(os.path.splitext(video)[0] + ".da.srt")
    db.upsert("movie:1", status="forsoeg_igen", batch_id="msgbatch_gammel")
    claude.batches = GoneBatches(danish, clock, db, how=how)
    claude.requests.clear()
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert "Den gemte batch msgbatch_gammel kan ikke bruges" in caplog.text
    assert "feltet nulstilles, og bidderne sendes som en ny batch" in caplog.text
    assert claude.batches.creates == 1 and direct_firsts(claude) == [1]
    assert set(claude.batches.seen_ids) == {"msgbatch_1"}  # det nye id er gemt under ventetiden
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["batch_id"] is None
    assert "batch: 2 bidder, 2 lykkedes, 0 direkte" in done_line(caplog)


# --- BATCH_API=false, indstillinger og database ---------------------------------------------------------
def test_batch_api_off_gives_the_direct_flow(arr, tmp_path, caplog):
    long_movie(arr)
    clock = Clock()
    claude = FakeAnthropic()  # ingen .batches: et forsøg på at bruge batch ville fejle
    r, db = runner(arr, tmp_path, claude=claude, clock=clock)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert direct_firsts(claude) == [1, 51, 101]
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["batch_id"] is None
    assert row["cost_usd"] == pytest.approx(len(claude.requests) * DIRECT)
    assert "batch" not in done_line(caplog)


def test_config_batch_keys():
    c = Config.from_env({})
    assert c.batch_api is False and c.batch_timeout_minutes == 90
    c = Config.from_env({"BATCH_API": "true", "BATCH_TIMEOUT_MINUTES": "45"})
    assert c.batch_api is True and c.batch_timeout_minutes == 45
    assert c.switches() == Config().switches()  # batch ændrer ikke kontakterne i rapporten


def test_old_database_gets_batch_id_column(tmp_path):
    path = str(tmp_path / "gammel.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (key TEXT PRIMARY KEY, media_type TEXT NOT NULL, media_id INTEGER NOT NULL, "
        "title TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT)"
    )
    conn.execute("INSERT INTO items (key, media_type, media_id, status) VALUES ('movie:1', 'movie', 1, 'faerdig')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.get("movie:1")["batch_id"] is None and db.get("movie:1")["status"] == "faerdig"
    db.upsert("movie:1", batch_id="msgbatch_1")
    assert db.get("movie:1")["batch_id"] == "msgbatch_1"


def test_start_log_mentions_batch(caplog):
    from oversaetter.runner import Runner

    c = Config(batch_api=True, batch_timeout_minutes=60)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        Runner(c, Database(":memory:"), claude=object()).log_start()
    assert "BATCH_API=true: bid 2 og frem sendes som én Message Batch (halv pris), højst 60 min ventetid" in caplog.text
