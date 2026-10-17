"""Fejl fra rigtig drift (Spa Weekend, 17.10.2026): afviste bidder, kald der hænger, og uventede fejl i løkken."""

import logging
import os
import signal
import threading
import time

import anthropic
import httpx2
import pytest

from oversaetter import clients
from oversaetter.checks import EnglishIndex
from oversaetter.clients import ApiError, Bazarr
from oversaetter.postprocess import postprocess
from oversaetter.proofread import SYSTEM_PROMPT, proofread
from tests.fakes import FakeArr, en_sub
from tests.helpers import FakeBudget, FakeClaude, cfg, cue, response
from tests.test_runner import runner
from tests.test_selection import make_cfg


def make(n):
    en = [cue(i * 3 + 10, i * 3 + 12, f"Line {i} in English with {i} apples.") for i in range(n)]
    da = [cue(i * 3 + 10, i * 3 + 12, f"Replik {i} på dansk med {i} æbler.") for i in range(n)]
    return en, da


def to_srt(cues):
    def ts(ms):
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    return "\n".join(f"{n}\n{ts(c.start)} --> {ts(c.end)}\n{c.text}\n" for n, c in enumerate(cues, start=1))


def refusal():
    return response(text="", stop="refusal")


@pytest.fixture
def arr(tmp_path):
    a = FakeArr(str(tmp_path / "Media"))
    yield a
    a.release.set()


@pytest.fixture
def keep_signals():
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, handler in saved.items():
        signal.signal(s, handler)


def big_movie(arr, n=120):
    """Film med n replikker og en dansk fil fra Bazarr (med Bazarrs kreditlinje), som skrives ved translate."""
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en, da = make(n)
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(to_srt(en))
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    da_path = os.path.splitext(video)[0] + ".da.srt"
    haiku = [cue(1, 6, "# Subtitles translated with Lingarr #")] + [
        cue(c.start / 1000, c.end / 1000, c.text) for c in da
    ]

    def write(form):
        with open(da_path, "w", encoding="utf-8") as f:
            f.write(to_srt(haiku))

    arr.on_translate = write
    return da_path


# --- 1. refusal midt i filen ---------------------------------------------------------------
def test_refusal_in_the_middle_rest_is_fixed_and_file_is_saved(arr, tmp_path, caplog):
    da_path = big_movie(arr)
    claude = FakeClaude(
        [
            response([{"i": 3, "da": "Rettet tre"}]),  # replik 1-50
            refusal(),  # replik 51-100
            refusal(),  # 51-75
            refusal(),  # 76-100
            response([{"i": 120, "da": "Sidste rettet"}]),  # replik 101-120
        ]
    )
    r, db = runner(arr, tmp_path, claude=claude, proofread_batch=50)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(claude.requests) == 5
    text = open(da_path, encoding="utf-8").read()
    assert text.startswith("1\n00:00:01,000 --> ")
    assert "Oversat til dig af" in text and "Lingarr" not in text
    assert "Rettet tre" in text and "Sidste rettet" in text
    assert "Replik 60 på dansk" in text  # Haikus tekst beholdes i den afviste bid
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["fixes"] == 2
    assert "2 bidder fejlede (replik 51-75, 76-100)" in row["reason"]
    assert "prøver igen delt i to: 51-75 og 76-100" in caplog.text
    assert "Færdig: Film 1" in caplog.text


# --- 2. refusal der lykkes ved deling i to ---------------------------------------------------------
def test_refusal_succeeds_when_split_in_two():
    en, da = make(100)
    claude = FakeClaude(
        [
            response([]),
            refusal(),
            response([{"i": 60, "da": "Første halvdel"}]),
            response([{"i": 90, "da": "Anden halvdel"}]),
        ]
    )
    budget = FakeBudget()
    res = proofread(da, EnglishIndex(en), {}, cfg(proofread_batch=50), claude, budget)
    assert res.fixes == {59: "Første halvdel", 89: "Anden halvdel"}
    assert res.batches_failed == 0 and res.batches_ok == 3
    first_half, second_half = claude.lines(2), claude.lines(3)
    assert [x["i"] for x in first_half if not x.get("context")] == list(range(51, 76))
    assert [x["i"] for x in second_half if not x.get("context")] == list(range(76, 101))
    assert [x["i"] for x in second_half if x.get("context")] == [71, 72, 73, 74, 75]
    assert budget.calls == 4  # også det afviste kald tælles med i prisen


def test_refused_half_is_discarded_other_half_kept():
    en, da = make(50)
    claude = FakeClaude([refusal(), refusal(), response([{"i": 40, "da": "Reddet"}])])
    res = proofread(da, EnglishIndex(en), {}, cfg(proofread_batch=50), claude, FakeBudget())
    assert res.fixes == {39: "Reddet"}
    assert res.batches_failed == 1 and res.failed_ranges == ["1-25"]


# --- 3. timeout fra Anthropic --------------------------------------------------------------------
def test_anthropic_timeout_skips_batch_and_continues():
    en, da = make(100)
    timeout = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    claude = FakeClaude([timeout, response([{"i": 80, "da": "Efter timeout"}])])
    res = proofread(da, EnglishIndex(en), {}, cfg(proofread_batch=50), claude, FakeBudget())
    assert res.fixes == {79: "Efter timeout"}
    assert res.failed_ranges == ["1-50"]


def test_anthropic_call_that_never_answers_is_abandoned(caplog):
    en, da = make(100)
    release = threading.Event()

    def reply(kwargs):
        if len(claude.requests) == 1:
            release.wait(30)  # hænger
        return response([{"i": 80, "da": "Næste bid"}])

    claude = FakeClaude(reply)
    started = time.monotonic()
    try:
        with caplog.at_level(logging.INFO, logger="oversaetter"):
            c = cfg(proofread_batch=50, proofread_call_timeout_minutes=0.001)
            res = proofread(da, EnglishIndex(en), {}, c, claude, FakeBudget())
    finally:
        release.set()
    assert time.monotonic() - started < 10
    assert res.fixes == {79: "Næste bid"} and res.failed_ranges == ["1-50"]
    assert "svarede ikke inden for" in caplog.text and "(timeout)" in caplog.text


# --- 4. Bazarr der ikke svarer --------------------------------------------------------------------
def test_bazarr_that_never_answers_gives_error_instead_of_hanging(arr, tmp_path, monkeypatch):
    monkeypatch.setattr(clients, "GET_TIMEOUT", 0.2)
    monkeypatch.setattr(clients, "JOBS_TIMEOUT", 0.2)
    monkeypatch.setattr(clients, "HARD_MARGIN", 0.0)
    arr.fail["bazarr"] = "hang"
    b = Bazarr(make_cfg(tmp_path), arr.client())
    started = time.monotonic()
    with pytest.raises(ApiError, match="svarede ikke inden for"):
        b.wanted_movies()
    assert b.jobs() is None
    with pytest.raises(httpx2.TimeoutException):
        b.translate("movie", 1, 0.2)
    assert time.monotonic() - started < 10


def test_round_with_hanging_bazarr_ends(arr, tmp_path, monkeypatch):
    monkeypatch.setattr(clients, "GET_TIMEOUT", 0.2)
    monkeypatch.setattr(clients, "JOBS_TIMEOUT", 0.2)
    monkeypatch.setattr(clients, "HARD_MARGIN", 0.0)
    big_movie(arr)
    arr.fail["bazarr"] = "hang"
    r, _ = runner(arr, tmp_path, claude=FakeClaude([]))
    started = time.monotonic()
    assert r.run_once() == []
    assert time.monotonic() - started < 15


# --- 5. samlet tidsgrænse overskredet -------------------------------------------------------------
def test_total_time_limit_saves_what_is_done_with_credit(caplog):
    en, da = make(200)
    now = [0.0]

    def reply(kwargs):
        now[0] += 20 * 60  # hver bid tager 20 minutter
        return response([{"i": len(claude.requests) * 50, "da": f"Rettet i bid {len(claude.requests)}"}])

    claude = FakeClaude(reply)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = postprocess(da, en, cfg(proofread_batch=50), claude, FakeBudget(), clock=lambda: now[0])
    assert len(claude.requests) == 2  # 0 -> 20 -> 40 minutter: stop før tredje bid
    assert res.proofread == "delvis (tidsgrænsen på 30 minutter nået)"
    assert res.fixes == 2
    assert res.cues[0].text.startswith("Oversat til dig af")
    assert res.cues[50].text == "Rettet i bid 1" and res.cues[100].text == "Rettet i bid 2"
    assert len(res.cues) == 201
    assert "ADVARSEL: Efterbehandlingen har brugt over 30 minutter" in caplog.text


def test_unexpected_error_in_proofread_still_gives_credit(monkeypatch):
    import oversaetter.postprocess as pp

    def boom(*a, **kw):
        raise RuntimeError("uventet")

    monkeypatch.setattr(pp, "proofread", boom)
    en, da = make(3)
    res = postprocess(da, en, cfg(), FakeClaude([]), FakeBudget())
    assert res.proofread == "fejlede uventet"
    assert res.cues[0].text.startswith("Oversat til dig af") and len(res.cues) == 4


def test_failed_postprocess_still_saves_file_with_credit(arr, tmp_path, monkeypatch):
    import oversaetter.runner as runner_mod

    da_path = big_movie(arr, n=5)

    def boom(*a, **kw):
        raise RuntimeError("noget gik galt")

    monkeypatch.setattr(runner_mod, "postprocess", boom)
    r, db = runner(arr, tmp_path, claude=FakeClaude([]))
    r.run_once()
    text = open(da_path, encoding="utf-8").read()
    assert "Oversat til dig af" in text and "Lingarr" not in text and "Replik 4 på dansk" in text
    assert db.get("movie:1")["status"] == "faerdig"


# --- 6. uventet fejl i hovedløkken -----------------------------------------------------------------
def test_unexpected_error_in_loop_marks_item_and_next_round_runs(arr, tmp_path, caplog, keep_signals):
    r, db = runner(arr, tmp_path, claude=FakeClaude([]), poll_minutes=0)
    db.upsert("movie:1", media_type="movie", media_id=1, title="Spa Weekend", status="i_gang")
    rounds = []

    def run_once():
        rounds.append(1)
        if len(rounds) == 1:
            r.current = ("movie:1", "Spa Weekend")
            raise KeyError("uventet nøgle")
        r.stopping = True
        return []

    r.resume = lambda: None
    r.run_once = run_once
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.loop()
    assert len(rounds) == 2
    row = db.get("movie:1")
    assert row["status"] == "forsoeg_igen" and "KeyError" in row["reason"]
    assert "Uventet fejl i hovedløkken: KeyError" in caplog.text
    assert "Traceback" in caplog.text  # fuld fejlbesked
    assert "Spa Weekend er markeret som forsoeg_igen" in caplog.text


# --- 7. genoptagelse af i_gang ved opstart ---------------------------------------------------------
def test_startup_resumes_items_stuck_in_progress(arr, tmp_path, caplog, keep_signals):
    da_path = big_movie(arr, n=5)
    arr.on_translate(None)  # Bazarrs fil findes allerede
    en_path = arr.movies[1]["subtitles"][0]["path"]
    r, db = runner(arr, tmp_path, claude=FakeClaude([response([])]), poll_minutes=0)
    db.upsert(
        "movie:1",
        media_type="movie",
        media_id=1,
        title="Spa Weekend",
        status="i_gang",
        attempts=1,
        video_path=arr.movies[1]["path"],
        source=en_path,
        da_path=da_path,
        started_at="2026-10-17T20:00:00+00:00",
    )
    db.conn.execute("UPDATE items SET updated_at = '2026-10-17T20:13:46+00:00' WHERE key = 'movie:1'")
    db.conn.commit()

    def run_once():
        r.stopping = True
        return []

    r.run_once = run_once
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.loop()
    assert "Spa Weekend har stået som i_gang i over 1 time" in caplog.text
    assert "Genoptager efterbehandling af Spa Weekend" in caplog.text
    assert db.get("movie:1")["status"] == "faerdig"
    assert "Oversat til dig af" in open(da_path, encoding="utf-8").read()


# --- skærpet korrekturinstruks --------------------------------------------------------------------
def test_prompt_forbids_notes_with_example():
    system = SYSTEM_PROMPT.format(max_chars=40)
    assert "ONLY the finished Danish subtitle" in system
    assert "Wrong:" in system and "Right:" in system
    assert '"da": "Vent, jeg er straks tilbage."}' in system


def test_rejected_fix_text_is_logged(caplog):
    en, da = make(2)
    claude = FakeClaude([response([{"i": 1, "da": "Hej (translation: hi)"}])])
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = proofread(da, EnglishIndex(en), {}, cfg(), claude, FakeBudget())
    assert res.rejected == 1
    assert "indeholder engelske noter: 'Hej (translation: hi)'" in caplog.text
