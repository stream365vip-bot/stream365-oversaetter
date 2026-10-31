"""Trin B7 (31.10.2026): send hver færdig eller fejlet oversættelse til appens backend (RSS-widget).

Alle tekster er selvskrevne.
"""

import json
import logging
import os
import threading

import httpx2
import pytest

from oversaetter import feed
from oversaetter.config import Config
from oversaetter.feed import build_payload, split_episode
from tests.fakes import FakeArr, en_sub
from tests.test_direct import EN, FakeAnthropic, movie_with_english, runner

FEED = "http://feed.test/oversaettelser"
TOKEN = "feed-token-123"


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


class FakeFeed:
    """Falsk backend: noterer hvert POST og svarer med status (eller "connect"/"hang")."""

    def __init__(self, status: int | str = 201):
        self.status = status
        self.posts: list[dict] = []  # {"json", "token"}
        self.release = threading.Event()

    def handle(self, req: httpx2.Request) -> httpx2.Response:
        self.posts.append({"json": json.loads(req.content), "token": req.headers.get("X-Oversaetter-Token")})
        status = self.status(len(self.posts)) if callable(self.status) else self.status
        if status == "connect":
            raise httpx2.ConnectError("nope", request=req)
        if status == "hang":
            self.release.wait(30)
            return httpx2.Response(503)
        return httpx2.Response(status, json={"ok": status < 300})


def feed_runner(arr, tmp_path, fake: FakeFeed | None, token=TOKEN, **kw):
    """Runner med FEED_URL og tokenfil. fake=None: FEED_URL er tom (slået fra)."""
    handle = arr.handle
    seen = fake or FakeFeed()
    arr.handle = lambda req: seen.handle(req) if req.url.host == "feed.test" else handle(req)
    token_file = tmp_path / "feed_token.txt"
    token_file.write_text(token + "\n", encoding="utf-8")
    url = FEED if fake is not None else ""
    r, db = runner(arr, tmp_path, feed_url=url, feed_token_file=str(token_file), **kw)
    return r, db, seen


# --- rene funktioner -----------------------------------------------------------------------------------------
def test_episode_label_is_split_from_title():
    assert split_episode("The Fix 5x02") == ("The Fix", "S5E2")
    assert split_episode("Serie 50 1x05") == ("Serie 50", "S1E5")
    assert split_episode("Idiots") == ("Idiots", "")


def test_payload_fields_and_dkk():
    cfg = Config(usd_to_dkk=6.7)
    p = build_payload(cfg, "Idiots", "movie", "done", 1412, 173, 1.2834, "11 flag")
    assert p == {
        "title": "Idiots",
        "media_type": "movie",
        "status": "done",
        "cues": 1412,
        "minutes": 3,
        "cost_usd": 1.2834,
        "cost_dkk": 8.6,
        "note": "11 flag",
    }
    e = build_payload(cfg, "The Fix 5x02", "episode", "failed", 0, 20, 0.0, "ingen engelsk")
    assert (e["title"], e["media_type"], e["episode_label"], e["minutes"], e["cost_dkk"]) == (
        "The Fix",
        "episode",
        "S5E2",
        0,
        0.0,
    )


def test_config_reads_feed_settings():
    cfg = Config.from_env({"FEED_URL": f" {FEED} ", "FEED_TOKEN_FILE": "/x/token", "USD_TO_DKK": "7,1"})
    assert (cfg.feed_url, cfg.feed_token_file, cfg.usd_to_dkk) == (FEED, "/x/token", 7.1)
    default = Config.from_env({})
    assert (default.feed_url, default.feed_token_file, default.usd_to_dkk) == ("", "/run/secrets/feed_token", 6.7)
    assert Config.from_env({"FEED_URL": ""}).feed_url == ""


# --- i driften -----------------------------------------------------------------------------------------------
def test_done_title_is_posted_with_the_right_fields(arr, tmp_path, caplog):
    movie_with_english(arr)
    fake = FakeFeed(201)
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "faerdig"
    assert len(fake.posts) == 1
    post = fake.posts[0]
    assert post["token"] == TOKEN
    body = post["json"]
    assert set(body) == {"title", "media_type", "status", "cues", "minutes", "cost_usd", "cost_dkk", "note"}
    assert (body["title"], body["media_type"], body["status"], body["cues"]) == ("Film 1", "movie", "done", 3)
    assert body["cost_usd"] == round(row["cost_usd"], 4) and body["cost_usd"] > 0
    assert body["cost_dkk"] == round(row["cost_usd"] * 6.7, 2)
    assert body["note"] == f"{row['flags_before']} flag"
    assert isinstance(body["minutes"], int)
    assert "Feed: sendt Film 1" in caplog.text
    assert TOKEN not in caplog.text


def test_episode_gets_series_title_and_label(arr, tmp_path):
    video = arr.add_episode(5, 50, 500, {"days": 2}, [], tags=[7], season=3)
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[5]["subtitles"] = [en_sub(en_path)]
    fake = FakeFeed(201)
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic())
    r.run_once()
    assert db.get("episode:5")["status"] == "faerdig"
    body = fake.posts[0]["json"]
    assert (body["title"], body["media_type"], body["episode_label"]) == ("Serie 50", "episode", "S3E5")


def test_hi_warning_is_the_note(arr, tmp_path):
    movie_with_english(arr)
    handle = arr.handle

    def wrapped(req):
        resp = handle(req)
        if req.method == "POST" and req.url.host == "bazarr":  # lige efter uploaden: Bazarr siger HI
            for s in arr.movies[1]["subtitles"]:
                if s.get("code2") == "da":
                    s["hi"] = True
        return resp

    arr.handle = wrapped
    fake = FakeFeed(201)
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic())
    r.run_once()
    assert fake.posts[0]["json"]["note"] == "Bazarr har registreret den danske tekst som HI"


def test_failed_title_is_posted_once_with_the_reason(arr, tmp_path):
    movie_with_english(arr)
    fake = FakeFeed(201)
    claude = FakeAnthropic(translate=lambda kw: RuntimeError("API nede"))
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=claude)
    r.run_once()
    assert db.get("movie:1")["status"] == "forsoeg_igen"
    assert fake.posts == []  # første fejl prøves igen - ingen linje i feedet endnu
    r.run_once()
    row = db.get("movie:1")
    assert row["status"] == "fejlet"
    assert len(fake.posts) == 1
    body = fake.posts[0]["json"]
    assert (body["title"], body["media_type"], body["status"]) == ("Film 1", "movie", "failed")
    assert body["note"] == row["reason"] == "ingen replikker blev oversat (se fejlene ovenfor)"
    assert body["cues"] == 3


def test_server_error_is_retried_once_then_warning(arr, tmp_path, caplog):
    movie_with_english(arr)
    fake = FakeFeed(500)
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(fake.posts) == 2  # ét genforsøg
    assert db.get("movie:1")["status"] == "faerdig"
    warnings = [x.getMessage() for x in caplog.records if x.levelno == logging.WARNING and "Feed" in x.getMessage()]
    assert warnings == ["Feed: kunne ikke sende Film 1 (HTTP 500)"]
    assert "Feed: sendt" not in caplog.text


def test_retry_that_succeeds_logs_sent(arr, tmp_path, caplog):
    movie_with_english(arr)
    fake = FakeFeed(lambda n: "connect" if n == 1 else 201)
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(fake.posts) == 2
    assert "Feed: sendt Film 1" in caplog.text
    assert "Feed: kunne ikke" not in caplog.text


def test_unauthorized_gives_token_hint_without_retry(arr, tmp_path, caplog):
    movie_with_english(arr)
    fake = FakeFeed(401)
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic(), token="forkert")
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert len(fake.posts) == 1  # et forkert token bliver ikke rigtigt af at prøve igen
    assert db.get("movie:1")["status"] == "faerdig"
    warning = [x for x in caplog.records if x.levelno == logging.WARNING and "Feed" in x.getMessage()][0]
    assert "HTTP 401" in warning.getMessage() and "OVERSAETTER_TOKEN" in warning.getMessage()
    assert "feed_token.txt" in warning.getMessage()
    assert "forkert" not in caplog.text


def test_hanging_backend_never_blocks_the_title(arr, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(feed, "FEED_TIMEOUT", 0.2)
    movie_with_english(arr)
    fake = FakeFeed("hang")
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic())
    try:
        with caplog.at_level(logging.INFO, logger="oversaetter"):
            r.run_once()
    finally:
        fake.release.set()
    assert db.get("movie:1")["status"] == "faerdig"
    assert "Feed: kunne ikke sende Film 1 (intet svar inden for 0.2 sekunder)" in caplog.text


def test_missing_token_gives_warning_and_no_post(arr, tmp_path, caplog):
    movie_with_english(arr)
    fake = FakeFeed(201)
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic(), token="")
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()
    assert fake.posts == [] and db.get("movie:1")["status"] == "faerdig"
    assert "Feed: intet token i" in caplog.text


def test_empty_feed_url_sends_nothing(arr, tmp_path, caplog):
    movie_with_english(arr)
    r, db, seen = feed_runner(arr, tmp_path, None, claude=FakeAnthropic())
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.log_start()
        r.run_once()
    assert seen.posts == [] and db.get("movie:1")["status"] == "faerdig"
    lines = [x.getMessage() for x in caplog.records if x.getMessage().startswith("Feed")]
    assert lines == ["Feed: FEED_URL er tom - færdige og fejlede oversættelser sendes ikke til appen"]


def test_dry_run_sends_nothing(arr, tmp_path):
    movie_with_english(arr)
    fake = FakeFeed(201)
    r, db, _ = feed_runner(arr, tmp_path, fake, claude=FakeAnthropic(), dry_run=True)
    r.run_once()
    assert fake.posts == []


def test_oversaet_test_never_uses_the_feed():
    """oversaet-test kører uden runneren og må aldrig sende til appen."""
    import oversaetter.oversaet as oversaet

    src = open(oversaet.__file__, encoding="utf-8").read()
    assert "feed" not in src.lower()
