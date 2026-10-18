"""TRANSLATE_MODE=direct: programmet oversætter selv med Claude og afleverer til Bazarr. Alle tekster er selvskrevne."""

import json
import logging
import os
import subprocess
from types import SimpleNamespace

import pytest

from oversaetter import direct
from oversaetter.config import Config, model_price
from oversaetter.db import Database
from oversaetter.direct import FIX_PROMPT, pick_stream
from oversaetter.oversaet import SYSTEM_PROMPT, translate_all
from oversaetter.runner import Runner
from tests.fakes import FakeArr, embedded, en_sub
from tests.helpers import srt, system_text
from tests.test_selection import make_cfg

EN = srt(
    ("00:00:10,000", "00:00:12,000", "Where is the car?"),
    ("00:00:13,000", "00:00:15,000", "I have 3 sisters."),
    ("00:00:16,000", "00:00:18,000", "Come with me, please."),
)
DANISH = {
    "Where is the car?": "Hvor er bilen?",
    "I have 3 sisters.": "Jeg har 3 søstre.",
    "Come with me, please.": "Kom med mig, please.",  # "please" flages af den gratis kontrol
}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def payload(kwargs) -> dict:
    content = kwargs["messages"][0]["content"]
    return json.loads(content.split("\n\n", 1)[1])


def reply(text="", stop="end_turn", inp=1000, out=200):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(
            input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0
        ),
    )


class FakeAnthropic:
    """Falsk Anthropic-klient. translate(kwargs) og fix(kwargs) giver svaret (eller rejser en undtagelse)."""

    def __init__(self, translate=None, fix=None):
        self.requests: list[dict] = []
        self.messages = self
        self.translate = translate or self.danish
        self.fix = fix or (lambda kw: reply(json.dumps({"fixes": []})))

    @staticmethod
    def danish(kwargs):
        lines = [{"i": x["i"], "da": DANISH.get(x["en"], f"Dansk {x['i']}")} for x in payload(kwargs)["lines"]]
        return reply(json.dumps({"lines": lines}, ensure_ascii=False))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        answer = self.translate if system_text(kwargs["system"]) == SYSTEM_PROMPT else self.fix
        out = answer(kwargs)
        if isinstance(out, Exception):
            raise out
        return out

    def kinds(self) -> list[str]:
        return ["oversæt" if system_text(r["system"]) == SYSTEM_PROMPT else "ret" for r in self.requests]


class FakeCommands:
    """Falske ffprobe/ffmpeg: streams er ffprobes spor, track_srt teksten som ffmpeg trækker ud."""

    def __init__(self, streams=(), track_srt=EN):
        self.streams = list(streams)
        self.track_srt = track_srt
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        assert kwargs["timeout"] > 0  # alle kald har en tidsgrænse
        self.calls.append(args)
        if args[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": self.streams}).encode(), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=self.track_srt.encode(), stderr=b"")


def stream(index, codec="subrip", lang="eng", title=None, forced=0, hi=0):
    tags = {"language": lang, **({"title": title} if title else {})}
    return {
        "index": index,
        "codec_name": codec,
        "tags": tags,
        "disposition": {"forced": forced, "hearing_impaired": hi},
    }


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def movie_with_english(arr):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    return video, en_path


def runner(arr, tmp_path, claude=None, commands=None, clock=None, **kw):
    cfg = make_cfg(tmp_path, translate_mode="direct", **kw)
    db = Database(os.path.join(cfg.data_dir, "oversaetter.db"))
    clock = clock or Clock()

    def no_lingarr(*a, **k):
        raise AssertionError("Bazarr/Lingarr må ikke bedes oversætte i direct-tilstand")

    r = Runner(
        cfg,
        db,
        http=arr.client(),
        claude=claude,
        translate=no_lingarr,
        sleep=clock.sleep,
        clock=clock,
        run_cmd=commands or FakeCommands(),
    )
    return r, db


def da_text(video) -> str:
    return open(os.path.splitext(video)[0] + ".da.srt", encoding="utf-8").read()


# --- ekstern engelsk .srt --------------------------------------------------------------------------
def test_external_english_is_translated_fixed_and_uploaded(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    claude = FakeAnthropic(fix=lambda kw: reply(json.dumps({"fixes": [{"i": 3, "da": "Kom med mig."}]})))
    commands = FakeCommands()
    r, db = runner(arr, tmp_path, claude=claude, commands=commands)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert claude.kinds() == ["oversæt", "ret"]
    assert claude.requests[0]["model"] == "claude-opus-5-5"
    assert claude.requests[0]["output_config"]["effort"] == "medium"
    assert commands.calls == []  # ekstern fil: ingen ffmpeg
    assert arr.translate_calls == []
    # Kun søgningen og uploaden ændrer noget i Bazarr
    assert arr.writes == [("PATCH", "bazarr/api/movies/subtitles"), ("POST", "bazarr/api/movies/subtitles")]
    up = arr.uploads[0]
    assert up["query"] == {"radarrid": "1"}
    assert (up["language"], up["forced"], up["hi"]) == ("da", "false", "false")
    assert up["filename"] == "Film 1.da.srt"
    text = da_text(video)
    assert text.startswith('1\n00:00:01,000 --> 00:00:06,000\nOversat til dig af\n<font color="#58D9AE">≋ </font>')
    assert "Hvor er bilen?" in text and "Jeg har 3 søstre." in text and "Kom med mig." in text
    assert "please" not in text
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["fixes"] == 1 and row["flags_before"] == 1 and row["flags_after"] == 0
    assert "uploadet til Bazarr" in row["reason"]
    expected = model_price("claude-opus-5-5", 2000, 400)
    assert db.month_spend() == pytest.approx(expected) and row["cost_usd"] == pytest.approx(expected)
    done = next(rec.message for rec in caplog.records if rec.message.startswith("Færdig:"))
    for part in ("model: claude-opus-5-5", "replikker: 3", "flag: 1", "rettelser: 1", "pris: 0.", "tid: "):
        assert part in done
    # Anden runde: intet sker
    r.run_once()
    assert len(claude.requests) == 2 and len(arr.uploads) == 1


def test_fix_round_sends_flagged_lines_with_neighbours_and_validates(arr, tmp_path, caplog):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    cues = [
        (f"00:00:{10 + i * 3:02d},000", f"00:00:{11 + i * 3:02d},000", f"Line number {i + 1} here.") for i in range(9)
    ]
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(srt(*cues))
    arr.movies[1]["subtitles"] = [en_sub(en_path)]

    def translate(kwargs):
        lines = [
            {"i": x["i"], "da": f"Replik {x['i']}, please." if x["i"] in (5, 9) else f"Replik {x['i']}."}
            for x in payload(kwargs)["lines"]
        ]
        return reply(json.dumps({"lines": lines}))

    fixes = [
        {"i": 5, "da": "Replik 5, tak."},
        {"i": 9, "da": "Replik 9 (Note: please translated)"},
        {"i": 2, "da": "X"},
    ]
    claude = FakeAnthropic(translate=translate, fix=lambda kw: reply(json.dumps({"fixes": fixes})))
    r, db = runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    fix_req = claude.requests[-1]
    assert fix_req["model"] == "claude-opus-5-5" and fix_req["system"].startswith(FIX_PROMPT[:40])
    lines = payload(fix_req)["lines"]
    assert [x["i"] for x in lines if x.get("fix")] == [5, 9]
    assert [x["i"] for x in lines if x.get("context")] == [3, 4, 6, 7, 8]
    text = da_text(video)
    assert "Replik 5, tak." in text and "Replik 9, please." in text and "Replik 2." in text
    assert db.get("movie:1")["fixes"] == 1
    assert "Rettelse kasseret" in caplog.text and "Rettelsesrunde: 1 af 2 flagede replikker rettet" in caplog.text


def test_fix_round_is_limited_to_max_fix_lines(arr, tmp_path):
    movie_with_english(arr)
    claude = FakeAnthropic(
        translate=lambda kw: reply(
            json.dumps({"lines": [{"i": x["i"], "da": "Ja, please."} for x in payload(kw)["lines"]]})
        )
    )
    r, _ = runner(arr, tmp_path, claude=claude, max_fix_lines=2)
    r.run_once()
    assert [x["i"] for x in payload(claude.requests[-1])["lines"] if x.get("fix")] == [1, 2]


# --- indlejret spor via ffmpeg ---------------------------------------------------------------------
def test_embedded_track_is_extracted_with_ffmpeg(arr, tmp_path):
    video = arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    commands = FakeCommands(
        streams=[
            stream(2, lang="dan"),
            stream(3, title="English SDH"),
            stream(4, title="Forced", forced=1),
            stream(5, codec="hdmv_pgs_subtitle"),
            stream(6, codec="ass", title="English"),
            stream(7),
        ]
    )
    claude = FakeAnthropic()
    r, db = runner(arr, tmp_path, claude=claude, commands=commands)
    r.run_once()
    assert [c[0] for c in commands.calls] == ["ffprobe", "ffmpeg"]
    ff = commands.calls[1]
    assert ff[ff.index("-map") + 1] == "0:6" and ff[ff.index("-i") + 1] == video and "srt" in ff
    assert "Hvor er bilen?" in da_text(video)
    assert db.get("movie:1")["status"] == "faerdig"


def test_only_image_based_english_is_skipped(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    commands = FakeCommands(streams=[stream(2, codec="hdmv_pgs_subtitle"), stream(3, codec="dvd_subtitle")])
    claude = FakeAnthropic()
    r, db = runner(arr, tmp_path, claude=claude, commands=commands)
    r.run_once()
    assert [c[0] for c in commands.calls] == ["ffprobe"]
    assert claude.requests == [] and arr.uploads == []
    row = db.get("movie:1")
    assert row["status"] == "ikke_muligt" and "billedbaseret" in row["reason"]
    r.run_once()
    assert len(commands.calls) == 1


def test_missing_ffmpeg_is_a_normal_failure(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])

    def missing(args, **kw):
        raise FileNotFoundError(args[0])

    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), commands=missing)
    r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "forsoeg_igen" and "ffprobe findes ikke" in row["reason"]


def test_ffmpeg_timeout_is_a_normal_failure(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])

    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), commands=slow)
    r.run_once()
    assert "blev ikke færdig" in db.get("movie:1")["reason"]


def test_pick_stream_prefers_non_sdh():
    picked, _ = pick_stream([stream(3, hi=1), stream(4, title="SDH"), stream(5, title="English")])
    assert picked["index"] == 5
    picked, _ = pick_stream([stream(3, title="SDH")])
    assert picked["index"] == 3  # kun SDH: så bruges det
    assert pick_stream([stream(3, lang="ger")]) == (None, "ingen engelsk tekstkilde i videoen")


# --- refusal -> halvering -> fallback-model ---------------------------------------------------------
def english(n):
    return [f"we talk about thing {i + 1}." for i in range(n)]


def opus_refuses_line_3(kwargs):
    nums = [x["i"] for x in payload(kwargs)["lines"]]
    if kwargs["model"] == "claude-opus-5-5" and 3 in nums:
        return reply(stop="refusal")
    return reply(json.dumps({"lines": [{"i": i, "da": f"Dansk {i}"} for i in nums]}))


def test_refusal_is_halved_then_sent_to_fallback_model(caplog):
    fake = FakeAnthropic(translate=opus_refuses_line_3)
    used = []
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = translate_all(
            english(10),
            "anthropic",
            fake,
            "claude-opus-5-5",
            "medium",
            50,
            60.0,
            fallback_model="claude-sonnet-5-5",
            on_usage=lambda m, *tokens: used.append(m),
        )
    calls = [(r["model"], [x["i"] for x in payload(r)["lines"]]) for r in fake.requests]
    assert calls == [
        ("claude-opus-5-5", list(range(1, 11))),
        ("claude-opus-5-5", list(range(1, 11))),
        ("claude-opus-5-5", [1, 2, 3, 4, 5]),
        ("claude-sonnet-5-5", [1, 2, 3, 4, 5]),
        ("claude-opus-5-5", [6, 7, 8, 9, 10]),
    ]
    assert res.da == [f"Dansk {i}" for i in range(1, 11)] and res.fallback == [] and res.by_fallback == 5
    assert used == [c[0] for c in calls]
    assert set(res.usage) == {"claude-opus-5-5", "claude-sonnet-5-5"}
    assert "afvist af claude-opus-5-5 også efter halvering - prøver med fallback-modellen claude-sonnet-5-5" in (
        caplog.text
    )


def test_refused_by_fallback_too_keeps_english_and_logs(caplog):
    def refuse_all_with_3(kwargs):
        nums = [x["i"] for x in payload(kwargs)["lines"]]
        if 3 in nums:
            return reply(stop="refusal")
        return reply(json.dumps({"lines": [{"i": i, "da": f"Dansk {i}"} for i in nums]}))

    fake = FakeAnthropic(translate=refuse_all_with_3)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        res = translate_all(
            english(10), "anthropic", fake, "claude-opus-5-5", "medium", 50, 60.0, fallback_model="claude-sonnet-5-5"
        )
    assert res.fallback == [0, 1, 2, 3, 4] and res.da[:5] == english(10)[:5] and res.da[5] == "Dansk 6"
    assert "beholder den engelske tekst" in caplog.text and "også claude-sonnet-5-5" in caplog.text


def test_refusal_with_fallback_in_full_flow_records_both_prices(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)

    def refuse_opus(kwargs):
        if kwargs["model"] == "claude-opus-5-5":
            return reply(stop="refusal")
        return FakeAnthropic.danish(kwargs)

    claude = FakeAnthropic(translate=refuse_opus)
    r, db = runner(arr, tmp_path, claude=claude)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert "Hvor er bilen?" in da_text(video)
    models = [rq["model"] for rq in claude.requests]
    assert models.count("claude-sonnet-5-5") == 2  # én for hver halvdel
    expected = model_price("claude-opus-5-5", 4000, 800) + model_price("claude-sonnet-5-5", 2000, 400)
    expected += model_price("claude-opus-5-5", 1000, 200)  # rettelsesrunden (please)
    assert db.month_spend() == pytest.approx(expected)
    assert "fallback-model: 3" in caplog.text


# --- aflevering ----------------------------------------------------------------------------------
def test_upload_for_episode_uses_series_and_episode_id(arr, tmp_path):
    video = arr.add_episode(5, 50, 500, {"days": 2}, [], tags=[7])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[5]["subtitles"] = [en_sub(en_path)]
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    up = arr.uploads[0]
    assert up["path"] == "/api/episodes/subtitles" and up["query"] == {"seriesid": "50", "episodeid": "5"}
    assert db.get("episode:5")["status"] == "faerdig"


def test_failed_upload_writes_file_and_asks_for_scan_disk(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    arr.upload_status = 500
    r, db = runner(arr, tmp_path, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(arr.uploads) == 1
    text = da_text(video)
    assert "Hvor er bilen?" in text and "Oversat til dig af" in text
    assert arr.scan_calls == [{"path": "/api/movies", "radarrid": "1", "action": "scan-disk"}]
    assert "ADVARSEL: Upload til Bazarr fejlede" in caplog.text
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and "scan disk" in row["reason"]


def test_upload_that_bazarr_never_saves_falls_back_to_file(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    arr.upload_saves = False
    clock = Clock()
    r, _ = runner(arr, tmp_path, claude=FakeAnthropic(), clock=clock)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert "Hvor er bilen?" in da_text(video)
    assert arr.scan_calls and "har ikke gemt filen" in caplog.text
    assert clock.t >= direct.UPLOAD_WAIT


def test_failed_upload_for_episode_scans_the_series(arr, tmp_path):
    video = arr.add_episode(5, 50, 500, {"days": 2}, [], tags=[7])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[5]["subtitles"] = [en_sub(en_path)]
    arr.upload_status = 500
    r, _ = runner(arr, tmp_path, claude=FakeAnthropic())
    r.run_once()
    assert arr.scan_calls == [{"path": "/api/series", "seriesid": "50", "action": "scan-disk"}]
    assert "Hvor er bilen?" in da_text(video)


# --- budget, fejl, prøvetilstand ----------------------------------------------------------------
def test_budget_reached_translates_nothing(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    claude = FakeAnthropic()
    r, db = runner(arr, tmp_path, claude=claude, monthly_budget_usd=1.0)
    db.add_spend(None, 0, 0, 1.0)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert claude.requests == [] and arr.uploads == []
    assert not os.path.exists(os.path.splitext(video)[0] + ".da.srt")
    assert db.get("movie:1")["status"] == "soegt" and db.get("movie:1")["attempts"] == 0
    assert "budget på 1.00 USD er nået" in caplog.text
    # Næste måned / højere loft: så oversættes der
    r.cfg.monthly_budget_usd = 10.0
    r.run_once()
    assert db.get("movie:1")["status"] == "faerdig"


def test_failed_translation_is_retried_once_then_gives_up(arr, tmp_path):
    movie_with_english(arr)
    claude = FakeAnthropic(translate=lambda kw: RuntimeError("API nede"))
    r, db = runner(arr, tmp_path, claude=claude)
    r.run_once()
    assert db.get("movie:1")["status"] == "forsoeg_igen" and arr.uploads == []
    r.run_once()
    assert db.get("movie:1")["status"] == "fejlet"


def test_dry_run_counts_lines_but_translates_nothing(arr, tmp_path, caplog):
    video, _ = movie_with_english(arr)
    r, db = runner(arr, tmp_path, claude=FakeAnthropic(), dry_run=True)
    db.upsert(
        "movie:1", media_type="movie", media_id=1, title="Film 1", status="soegt", search_at="2026-10-18T00:00:00"
    )
    claude = r.claude
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert "DRY_RUN: ville oversætte 3 replikker med claude-opus-5-5: Film 1" in caplog.text
    assert claude.requests == [] and arr.uploads == [] and arr.writes == []
    assert not os.path.exists(os.path.splitext(video)[0] + ".da.srt")
    assert db.get("movie:1")["status"] == "soegt" and db.month_spend() == 0


def test_resume_in_direct_mode_does_not_translate_again(arr, tmp_path):
    video, en_path = movie_with_english(arr)
    claude = FakeAnthropic()
    r, db = runner(arr, tmp_path, claude=claude)
    da = os.path.splitext(video)[0] + ".da.srt"
    db.upsert(
        "movie:1",
        media_type="movie",
        media_id=1,
        title="Film 1",
        status="i_gang",
        attempts=1,
        video_path=video,
        source=en_path,
        da_path=da,
        started_at="2020-01-01T00:00:00+00:00",
    )
    with open(da, "w", encoding="utf-8") as f:
        f.write(EN)
    r.resume()
    assert db.get("movie:1")["status"] == "faerdig" and claude.requests == []


# --- tilstand og indstillinger --------------------------------------------------------------------
def test_lingarr_mode_gives_the_old_flow(arr, tmp_path):
    video, _ = movie_with_english(arr)

    def haiku(form):
        with open(os.path.splitext(video)[0] + ".da.srt", "w", encoding="utf-8") as f:
            f.write(EN.replace("Where is the car?", "Hvor er bilen?"))

    arr.on_translate = haiku
    cfg = make_cfg(tmp_path, translate_mode="lingarr")
    db = Database(os.path.join(cfg.data_dir, "oversaetter.db"))
    from tests.test_runner import instant

    r = Runner(cfg, db, http=arr.client(), claude=None, translate=instant, run_cmd=FakeCommands())
    r.run_once()
    assert len(arr.translate_calls) == 1 and arr.uploads == []
    assert "Hvor er bilen?" in da_text(video)


def test_config_defaults_and_ignored_proofread_keys(caplog):
    c = Config.from_env({})
    assert (c.translate_mode, c.translate_model, c.translate_effort) == ("direct", "claude-opus-5-5", "medium")
    assert (c.fallback_model, c.max_fix_lines) == ("claude-sonnet-5-5", 60)
    c = Config.from_env({"PROOFREAD_MODEL": "claude-sonnet-5-5", "PROOFREAD_BATCH": "40", "TRANSLATE_MODE": "Direct"})
    assert c.direct and c.proofread_keys == ("PROOFREAD_MODEL", "PROOFREAD_BATCH")
    r = Runner(c, Database(":memory:"), claude=object())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.log_start()
    lines = [rec.message for rec in caplog.records if "PROOFREAD" in rec.message]
    assert lines == ["PROOFREAD_MODEL, PROOFREAD_BATCH i .env bruges ikke i TRANSLATE_MODE=direct og ignoreres"]
    assert not Config.from_env({"TRANSLATE_MODE": "lingarr"}).direct
    with pytest.raises(ValueError, match="TRANSLATE_MODE"):
        Config.from_env({"TRANSLATE_MODE": "haiku"})


def test_deadline_stops_translation_and_item_is_retried(arr, tmp_path, caplog):
    movie_with_english(arr)
    clock = Clock()

    def slow(kwargs):
        clock.t += 46 * 60  # ét kald bruger hele tidsgrænsen (TRANSLATE_TIMEOUT_MINUTES=45)
        return reply(stop="max_tokens")

    claude = FakeAnthropic(translate=slow)
    r, db = runner(arr, tmp_path, claude=claude, clock=clock)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(claude.requests) == 1 and arr.uploads == []
    row = db.get("movie:1")
    assert row["status"] == "forsoeg_igen" and "tidsgrænsen på 45 minutter" in row["reason"]
