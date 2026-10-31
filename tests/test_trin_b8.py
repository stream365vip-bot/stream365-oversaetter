"""Trin B8 (31.10.2026): fem rettelser efter driften - pause ved udgiftsgrænse/nøglefejl, 95 %-reglen, mærker hver
runde, Runde-linjen går op, glem-flaget og HI-mærket efter upload. Alt med fakes; alle tekster er selvskrevne.
"""

import json
import logging
import os

import anthropic
import httpx2
import pytest

from oversaetter.__main__ import forget
from oversaetter.db import Database
from oversaetter.konto import account_problem
from tests.fakes import FakeArr, en_sub
from tests.helpers import srt
from tests.test_direct import FakeAnthropic, da_text, movie_with_english, payload, reply, runner
from tests.test_selection import make_cfg
from tests.test_trin_b6 import danish, setup, ts

USAGE = "You have reached your specified API usage limits. You will regain access on 2026-11-01 at 00:00 UTC."


def api_error(status: int, error_type: str, message: str):
    """En rigtig anthropic-undtagelse (samme klasse og felter som SDK'en giver)."""
    resp = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    cls = {400: anthropic.BadRequestError, 401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError}
    return cls[status](f"Error code: {status} - {message}", response=resp, body=body)


def usage_limit():
    return api_error(400, "invalid_request_error", USAGE)


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def movie_lines(arr, n: int) -> str:
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(srt(*[(ts(10 + i * 3), ts(11 + i * 3), f"We talk about thing {i + 1}.") for i in range(n)]))
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    return video


def errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


def assert_untouched(arr, db, video, key="movie:1"):
    """Ingen upload, ingen dansk fil, status uændret (fra søgningen) og intet forsøg brugt."""
    row = db.get(key)
    assert arr.uploads == []
    assert not os.path.exists(os.path.splitext(video)[0] + ".da.srt")
    assert (row["status"], row["attempts"]) == ("soegt", 0)


# --- 1) udgiftsgrænse og nøglefejl -------------------------------------------------------------------------
def test_account_problem_recognises_only_account_errors():
    assert "udgiftsgrænsen" in account_problem(usage_limit()).reason
    credit = api_error(400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API.")
    assert "credits" in account_problem(credit).reason
    assert account_problem(api_error(401, "authentication_error", "invalid x-api-key")).kind == "nøglefejl"
    assert account_problem(api_error(403, "permission_error", "no access")).kind == "nøglefejl"
    assert account_problem(ConnectionError("netværk nede")) is None
    assert account_problem(TimeoutError("timeout")) is None
    assert account_problem(api_error(400, "invalid_request_error", "max_tokens is too large")) is None


def test_usage_limit_on_bid_1_pauses_without_upload_or_attempt(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    claude = FakeAnthropic(translate=lambda kw: usage_limit())
    r, db = runner(arr, tmp_path, claude=claude, account_pause_minutes=30)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert_untouched(arr, db, video)
    assert len(claude.requests) == 1  # ingen genforsøg, halvering eller fallback-model mod en lukket konto
    errs = errors(caplog)
    assert len(errs) == 1
    assert "udgiftsgrænsen er nået (usage limits)" in errs[0] and "afbrudt uden upload" in errs[0]
    assert "Pause i 30 minutter" in errs[0]
    assert "beholder den engelske tekst" not in caplog.text

    # Under pausen: ingen oversættelse og kun én loglinje pr. runde
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    lines = [rec.getMessage() for rec in caplog.records]
    assert len(lines) == 1 and lines[0].startswith("Pause pga. udgiftsgrænse, prøver igen kl. ")
    assert len(claude.requests) == 1

    # Efter pausen prøves igen; afvist igen -> ny pause, stadig uden forsøg brugt
    r.clock.t += 31 * 60
    r.run_once()
    assert len(claude.requests) == 2
    assert_untouched(arr, db, video)
    assert r.in_pause()

    # Kontoen virker igen -> oversat og uploadet som normalt
    claude.translate = FakeAnthropic.danish
    r.clock.t += 31 * 60
    r.run_once()
    row = db.get("movie:1")
    assert (row["status"], row["attempts"]) == ("faerdig", 1)
    assert len(arr.uploads) == 1 and "Hvor er bilen?" in da_text(video)


def test_401_pauses_the_same_way_as_key_error(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    claude = FakeAnthropic(translate=lambda kw: api_error(401, "authentication_error", "invalid x-api-key"))
    r, db = runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
        r.run_once()
    assert_untouched(arr, db, video)
    assert any("Anthropic afviser nøglen (HTTP 401)" in e for e in errors(caplog))
    assert "Pause pga. nøglefejl, prøver igen kl." in caplog.text


def test_usage_limit_in_fix_round_also_stops_the_title(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)  # "please" flages og sendes til rettelsesrunden
    claude = FakeAnthropic(fix=lambda kw: usage_limit())
    r, db = runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert claude.kinds() == ["oversæt", "ret"]
    assert_untouched(arr, db, video)
    assert r.in_pause()


def test_usage_limit_in_glossary_stops_before_the_bids(arr, tmp_path):
    video, _ = movie_with_english(arr)
    claude = FakeAnthropic(translate=lambda kw: usage_limit(), fix=lambda kw: usage_limit())
    r, db = runner(arr, tmp_path, claude=claude, glossary=True, prompt_version="v4")
    r.run_once()
    assert len(claude.requests) == 1  # kun ordliste-kaldet
    assert_untouched(arr, db, video)
    assert r.in_pause()


def test_usage_limit_in_the_middle_of_a_batch(arr, tmp_path, caplog):
    """Bid 1 lykkes direkte, batchens bidder fejler, og de direkte genforsøg rammer udgiftsgrænsen."""
    video = movie_lines(arr, 120)
    r, db, claude = setup(arr, tmp_path, answer=lambda p: "errored")
    claude.translate = lambda kw: FakeAnthropic.danish(kw) if payload(kw)["lines"][0]["i"] == 1 else usage_limit()
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert_untouched(arr, db, video)
    assert db.get("movie:1")["batch_id"] == "msgbatch_1"  # betalt batch: svarene hentes igen efter pausen
    assert any("udgiftsgrænsen er nået" in e for e in errors(caplog))
    assert r.in_pause()


def test_whole_batch_rejected_pauses_without_direct_retries(arr, tmp_path, caplog):
    video = movie_lines(arr, 120)
    r, db, claude = setup(arr, tmp_path, answer=danish)

    def rejected(requests):
        raise usage_limit()

    claude.batches.create = rejected
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(claude.requests) == 1  # kun bid 1 direkte - bid 2 og 3 sendes ikke direkte bagefter
    assert_untouched(arr, db, video)
    assert db.get("movie:1")["batch_id"] is None
    assert r.in_pause()


def test_network_error_is_handled_as_before(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    claude = FakeAnthropic(translate=lambda kw: ConnectionError("forbindelsen blev afbrudt"))
    r, db = runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert (row["status"], row["attempts"]) == ("forsoeg_igen", 1)
    assert row["reason"] == "ingen replikker blev oversat (se fejlene ovenfor)"
    assert arr.uploads == [] and not r.in_pause()
    assert "Pause pga." not in caplog.text
    assert len(claude.requests) > 1  # genforsøg og halvering som i dag


# --- 1b) 95 %-reglen -----------------------------------------------------------------------------------------
def leave_out(missing: set[int]):
    def translate(kw):
        lines = [{"i": x["i"], "da": f"Dansk {x['i']}"} for x in payload(kw)["lines"] if x["i"] not in missing]
        return reply(json.dumps({"lines": lines}))

    return translate


def test_title_with_80_percent_translated_is_not_uploaded(arr, tmp_path, caplog):
    video = movie_lines(arr, 10)
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(translate=leave_out({1, 2})))
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "forsoeg_igen"
    assert row["reason"] == "kun 8 af 10 replikker (80 %) blev oversat - under 95 %, intet uploadet"
    assert arr.uploads == [] and not os.path.exists(os.path.splitext(video)[0] + ".da.srt")
    assert any("kun 8 af 10 replikker" in e for e in errors(caplog))


def test_title_with_95_percent_translated_is_uploaded(arr, tmp_path):
    movie_lines(arr, 20)
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(translate=leave_out({7})))
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig" and len(arr.uploads) == 1


# --- 2) mærket læses hver runde ------------------------------------------------------------------------------
def test_new_tag_in_sonarr_is_seen_in_the_next_round(arr, tmp_path):
    arr.tags = [{"id": 1, "label": "andet"}]  # mærket findes ikke endnu
    video = arr.add_episode(5, 50, 500, {"days": 2}, [])
    with open(os.path.splitext(video)[0] + ".en.srt", "w", encoding="utf-8") as f:
        f.write(srt(("00:00:01,000", "00:00:02,000", "Hello.")))
    arr.episodes[5]["subtitles"] = [en_sub(os.path.splitext(video)[0] + ".en.srt")]
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert arr.search_calls == [] and db.get("episode:5") is None
    arr.tags.append({"id": 9, "label": "oversaet"})  # mærket oprettes og sættes på serien - ingen genstart
    arr.series[50]["tags"] = [9]
    r.run_once()
    assert len(arr.search_calls) == 1
    assert db.get("episode:5")["status"] == "faerdig"


# --- 3) Runde-linjen går op ----------------------------------------------------------------------------------
def test_round_line_counts_lookup_errors_and_unknown_in_bazarr(arr, tmp_path, caplog):
    arr.add_movie(1, 101, {"days": 2}, [])  # Seerr-ønske, men Bazarr kender den ikke ved opslaget
    arr.add_episode(5, 50, 500, {"days": 2}, [], tags=[7])
    handle = arr.handle

    def wrapped(req):
        if req.url.host == "bazarr" and req.url.path == "/api/movies" and req.method == "GET":
            return httpx2.Response(200, json={"data": []})
        if req.url.host == "sonarr" and "/series/" in req.url.path:
            return httpx2.Response(500, json={"error": "nej"})
        return handle(req)

    arr.handle = wrapped
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    line = next(rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("Runde:"))
    assert line.startswith("Runde: 2 mangler dansk, 0 oversættes")
    assert line.endswith("0 sprunget over, 1 fejl ved opslag, 1 ukendt i Bazarr")


# --- 4) glem ---------------------------------------------------------------------------------------------------
def _db(tmp_path, status):
    db = Database(str(tmp_path / "oversaetter.db"))
    db.upsert("movie:7", media_type="movie", media_id=7, title="Idiots", status=status)
    return db


@pytest.mark.parametrize("status", ["fejlet", "sprunget_over", "ikke_muligt"])
def test_forget_without_flag_for_failed_skipped_impossible(tmp_path, status):
    db = _db(tmp_path, status)
    assert forget(make_cfg(tmp_path), "Idiots", db=db) == 0
    assert db.get("movie:7") is None


def test_forget_done_title_is_refused_without_flag(tmp_path, caplog):
    db = _db(tmp_path, "faerdig")
    assert forget(make_cfg(tmp_path), "Idiots", db=db) == 1
    assert db.get("movie:7") is not None
    assert "har status faerdig - glem afvist" in caplog.text and "--alle" in caplog.text
    assert forget(make_cfg(tmp_path), "Idiots", db=db, all_statuses=True) == 0
    assert db.get("movie:7") is None


@pytest.mark.parametrize("status", ["forsoeg_igen", "i_gang", "soegt", "mangler_engelsk"])
def test_other_statuses_need_the_all_flag(tmp_path, caplog, status):
    db = _db(tmp_path, status)
    assert forget(make_cfg(tmp_path), "Idiots", db=db) == 1
    assert db.get("movie:7") is not None
    assert f"status {status} - glem afvist" in caplog.text
    assert forget(make_cfg(tmp_path), "Idiots", db=db, all_statuses=True) == 0
    assert db.get("movie:7") is None


def test_forget_flag_on_the_command_line(tmp_path, monkeypatch):
    from oversaetter import __main__ as main_mod

    seen = {}
    monkeypatch.setattr(
        main_mod, "forget", lambda cfg, what, all_statuses=False: seen.update(w=what, d=all_statuses) or 0
    )
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main_mod.main(["glem", "Idiots", "--alle"]) == 0
    assert seen == {"w": "Idiots", "d": True}


# --- 5) HI-mærke efter upload ----------------------------------------------------------------------------
def bazarr_marks_hi(arr, order=None):
    """Som på Gold Rush 15x2: Bazarr gemmer vores upload som .da.hi.srt og mærker den HI. Scan disk læser disken."""
    handle = arr.handle

    def wrapped(req):
        resp = handle(req)
        movie = arr.movies[1]
        base = os.path.splitext(movie["path"])[0]
        if req.url.host == "bazarr" and req.method == "POST":
            os.rename(base + ".da.srt", base + ".da.hi.srt")
            for s in movie["subtitles"]:
                if s.get("code2") == "da":
                    s.update(path=base + ".da.hi.srt", hi=True)
        if req.url.host == "bazarr" and req.method == "PATCH" and req.url.path == "/api/movies":
            if order is not None:
                order.append("scan")
            subs = [s for s in movie["subtitles"] if s.get("code2") != "da"]
            for name, hi in ((".da.srt", False), (".da.hi.srt", True)):
                if os.path.exists(base + name):
                    subs.append({"code2": "da", "code3": "dan", "path": base + name, "forced": False, "hi": hi})
            movie["subtitles"] = subs
        return resp

    arr.handle = wrapped


def test_hi_mark_after_upload_is_renamed_rescanned_and_saved(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    base = os.path.splitext(video)[0]
    order = []
    bazarr_marks_hi(arr, order)
    arr.on_subtitle_sync = lambda form: order.append(("sync", os.path.basename(form["path"])))
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert os.path.exists(base + ".da.srt") and not os.path.exists(base + ".da.hi.srt")
    assert order == [("sync", "Film 1.da.hi.srt"), "scan"]  # HI-tjekket kommer efter synken
    assert arr.scan_calls == [{"path": "/api/movies", "radarrid": "1", "action": "scan-disk"}]
    found = os.listdir(os.path.join(r.cfg.data_dir, "hi-fund"))
    assert found == ["movie-1-Film 1.da.hi.srt"]
    with open(os.path.join(r.cfg.data_dir, "hi-fund", found[0]), encoding="utf-8") as f:
        assert "Hvor er bilen?" in f.read()
    assert "Bazarr har registreret den danske tekst til Film 1 som HI" in caplog.text
    assert "HI-mærke: omdøbt Film 1.da.hi.srt til Film 1.da.srt" in caplog.text
    assert "HI-mærke rettet: Bazarr ser nu den danske tekst til Film 1 som almindelig" in caplog.text
    assert db.get("movie:1")["status"] == "faerdig"


def test_hi_find_keeps_at_most_10_files_and_does_not_overwrite_existing_da(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    base = os.path.splitext(video)[0]
    bazarr_marks_hi(arr)
    folder = str(tmp_path / "data" / "hi-fund")
    os.makedirs(folder)
    for i in range(10):
        open(os.path.join(folder, f"gammel-{i}.srt"), "w").close()
    handle = arr.handle

    def also_plain_da(req):  # en anden .da.srt dukker op, før vi når at omdøbe
        resp = handle(req)
        if req.url.host == "bazarr" and req.method == "POST":
            with open(base + ".da.srt", "w", encoding="utf-8") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nAnden\n")
        return resp

    arr.handle = also_plain_da
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    assert r.cfg.data_dir == str(tmp_path / "data")
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(os.listdir(folder)) == 10
    assert os.path.exists(base + ".da.hi.srt")  # ikke omdøbt: .da.srt findes allerede
    assert "er ikke omdøbt" in caplog.text
    assert len(arr.scan_calls) == 1  # Bazarr bedes stadig scanne og tjekkes igen


def test_no_hi_mark_means_no_rescan(arr, tmp_path):
    movie_with_english(arr)
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert arr.scan_calls == [] and not os.path.exists(os.path.join(r.cfg.data_dir, "hi-fund"))


def test_still_hi_after_rescan_is_warned(arr, tmp_path, caplog):
    movie_with_english(arr)
    handle = arr.handle

    def wrapped(req):  # Bazarr mærker HI og bliver ved med det, også efter scan disk
        resp = handle(req)
        if req.url.host == "bazarr" and req.method == "POST":
            for s in arr.movies[1]["subtitles"]:
                if s.get("code2") == "da":
                    s["hi"] = True
        return resp

    arr.handle = wrapped
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(arr.scan_calls) == 1
    assert "Bazarr ser stadig ikke den danske tekst til Film 1 som almindelig" in caplog.text
    assert db.get("movie:1")["status"] == "faerdig"


def test_config_account_pause_minutes():
    from oversaetter.config import Config

    assert Config.from_env({}).account_pause_minutes == 30
    assert Config.from_env({"ACCOUNT_PAUSE_MINUTES": "45"}).account_pause_minutes == 45


# --- 6) ingen engelsk kilde: scan disk én gang, før der gives op ----------------------------------------------
def scan_reveals(arr, subtitles):
    """Som Gold Rush sæson 17: Bazarr har ikke indekseret sporet, før den bedes om Scan Disk for serien."""
    handle = arr.handle

    def wrapped(req):
        resp = handle(req)
        if req.url.host == "bazarr" and req.method == "PATCH" and req.url.path == "/api/series":
            arr.episodes[5]["subtitles"] = list(subtitles)
        return resp

    arr.handle = wrapped


def test_no_english_asks_for_scan_disk_and_translates_embedded_sdh(arr, tmp_path, caplog):
    from tests.fakes import embedded
    from tests.test_direct import FakeCommands, stream

    arr.add_episode(5, 50, 500, {"days": 2}, [], tags=[7], season=17)  # første opslag: ingen engelsk
    scan_reveals(arr, [{**embedded("en", 11, track=3), "hi": True}])
    commands = FakeCommands(streams=[stream(3, title="English (SDH)", hi=1)])
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), commands=commands)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert arr.scan_calls == [{"path": "/api/series", "seriesid": "50", "action": "scan-disk"}]
    assert "beder Bazarr scanne disken og tjekker igen" in caplog.text
    assert "Efter scan disk har Bazarr en engelsk kilde" in caplog.text
    row = db.get("episode:5")
    assert row["status"] == "faerdig" and row["disk_scan_at"] and row["en_hi"] == 1
    assert len(arr.uploads) == 1
    assert not any(c.get("language") == "en" for c in arr.search_calls)  # ingen søgning efter engelsk


def test_scan_disk_for_missing_english_happens_only_once_per_title(arr, tmp_path, caplog):
    arr.add_episode(5, 50, 500, {"days": 2}, [], tags=[7])
    scan_reveals(arr, [])  # scanningen finder heller intet
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
        r.run_once()
        r.run_once()
    assert len(arr.scan_calls) == 1
    assert "Heller ingen engelsk tekstkilde til Serie 50 1x05 efter scan disk" in caplog.text
    assert db.get("episode:5")["status"] == "mangler_engelsk"
    assert arr.uploads == []
