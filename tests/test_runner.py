import os

import httpx2
import pytest

from oversaetter.__main__ import run_test
from oversaetter.clients import Bazarr
from oversaetter.db import Database
from oversaetter.runner import Runner
from oversaetter.selection import Candidate
from oversaetter.translate import Outcome, request_translation
from tests.fakes import FakeArr, embedded, en_sub
from tests.helpers import FakeBudget, FakeClaude, response, srt
from tests.test_selection import make_cfg

EN = srt(
    ("00:00:10,000", "00:00:12,000", "Where is the car?"),
    ("00:00:13,000", "00:00:15,000", "I have 3 sisters."),
)
HAIKU = srt(
    ("00:00:01,000", "00:00:06,000", "# Subtitles translated with Lingarr # "),
    ("00:00:10,000", "00:00:12,000", "Hvor er bilen?"),
    ("00:00:13,000", "00:00:15,000", "Jeg har tre søstre."),
)


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def movie_with_english(arr, days=2):
    video = arr.add_movie(1, 101, {"days": days}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    return video, en_path


def write_haiku(video):
    def go(form):
        with open(os.path.splitext(video)[0] + ".da.srt", "w", encoding="utf-8") as f:
            f.write(HAIKU)

    return go


def snapshot(directory):
    out = {}
    for root, _, files in os.walk(directory):
        for name in files:
            p = os.path.join(root, name)
            out[p] = (os.path.getmtime(p), open(p, "rb").read())
    return out


def instant(bazarr, cand, minutes):
    return request_translation(bazarr, cand, minutes, sleep=lambda s: None)


def runner(arr, tmp_path, claude=None, **kw):
    cfg = make_cfg(tmp_path, **kw)
    db = Database(os.path.join(cfg.data_dir, "oversaetter.db"))
    return Runner(cfg, db, http=arr.client(), claude=claude, translate=instant), db


def test_dry_run_calls_nothing_and_writes_nothing(arr, tmp_path):
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    arr.add_movie(2, 102, {"days": 2}, [], requested=False, tags=[3])
    arr.bazarr_unknown.add("movie:2")  # nyt mærke, som Bazarr ikke kender endnu -> ville synkronisere
    before = snapshot(arr.media_dir)
    r, db = runner(arr, tmp_path, claude=FakeClaude([]), dry_run=True)
    cands = r.run_once()
    assert cands == []
    assert arr.translate_calls == [] and arr.search_calls == [] and arr.sync_calls == [] and arr.writes == []
    assert snapshot(arr.media_dir) == before
    assert db.get("movie:1") is None


def test_full_flow_translates_proofreads_and_writes(arr, tmp_path):
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    claude = FakeClaude([response([{"i": 2, "da": "Jeg har 3 søstre."}])])
    r, db = runner(arr, tmp_path, claude=claude)
    r.run_once()
    assert arr.translate_calls == [
        {
            "action": "translate",
            "language": "da",
            "type": "movie",
            "id": "1",
            "path": arr.movies[1]["subtitles"][0]["path"],
        }
    ]
    # søgningen efter dansk og oversættelsen er de eneste ændrende kald
    assert arr.writes == [("PATCH", "bazarr/api/movies/subtitles"), ("PATCH", "bazarr/api/subtitles")]
    assert arr.search_calls == [
        {"path": "/api/movies/subtitles", "language": "da", "forced": "False", "hi": "False", "radarrid": "1"}
    ]
    da_path = os.path.splitext(video)[0] + ".da.srt"
    text = open(da_path, encoding="utf-8").read()
    assert text.startswith("1\n00:00:01,000 --> 00:00:06,000\nOversat af Claude for ≋\n")
    assert "Jeg har 3 søstre." in text and "Lingarr" not in text
    row = db.get("movie:1")
    assert row["status"] == "faerdig" and row["fixes"] == 1 and row["flags_before"] == 1 and row["flags_after"] == 0
    assert db.month_spend() > 0
    # Ingen andre filer er rørt eller oprettet
    assert sorted(os.listdir(os.path.dirname(video))) == sorted(
        [os.path.basename(video), os.path.basename(da_path), os.path.basename(video)[:-4] + ".en.srt"]
    )
    assert row["search_at"]
    # Anden runde: intet sker
    arr.translate_calls.clear()
    r.run_once()
    assert arr.translate_calls == [] and len(arr.search_calls) == 1


def test_budget_reached_keeps_haiku_text(arr, tmp_path):
    video, _ = movie_with_english(arr)
    arr.on_translate = write_haiku(video)
    claude = FakeClaude([])
    r, db = runner(arr, tmp_path, claude=claude, monthly_budget_usd=1.0)
    db.add_spend(None, 0, 0, 1.0)
    r.run_once()
    assert claude.requests == []
    text = open(os.path.splitext(video)[0] + ".da.srt", encoding="utf-8").read()
    assert "Jeg har tre søstre." in text and "Oversat af Claude for ≋" in text
    assert db.get("movie:1")["reason"] == "budget nået"


def test_failure_retries_once_then_gives_up(arr, tmp_path):
    movie_with_english(arr)
    arr.translate_status = 502
    r, db = runner(arr, tmp_path)
    r.run_once()
    assert db.get("movie:1")["status"] == "forsoeg_igen"
    r.run_once()
    assert db.get("movie:1")["status"] == "fejlet"
    r.run_once()
    assert len(arr.translate_calls) == 2


def test_image_based_track_is_skipped(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    arr.translate_status = 415
    r, db = runner(arr, tmp_path)
    r.run_once()
    assert arr.translate_calls[0]["subtitles_id"] == "42" and "path" not in arr.translate_calls[0]
    row = db.get("movie:1")
    assert row["status"] == "ikke_muligt" and "billedbaseret" in row["reason"]
    r.run_once()
    assert len(arr.translate_calls) == 1


def test_old_bazarr_without_embedded_support(arr, tmp_path):
    arr.add_movie(1, 101, {"days": 2}, [embedded("en", 42)])
    arr.translate_status = 501
    r, db = runner(arr, tmp_path)
    r.run_once()
    assert db.get("movie:1")["status"] == "ikke_muligt"


# --- vent på filen ------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def cand_for(video):
    return Candidate("movie", 1, "Film", "Seerr", video_path=video, source_path="/x.en.srt")


def test_background_job_file_appears_later(arr, tmp_path):
    video = arr.video("F/F.mkv")
    clock = Clock()
    da = os.path.splitext(video)[0] + ".da.srt"

    def sleep(s):
        clock.sleep(s)
        if clock.t >= 120 and not os.path.exists(da):
            with open(da, "w") as f:
                f.write(HAIKU)

    out = request_translation(Bazarr(make_cfg(tmp_path), arr.client()), cand_for(video), 45, sleep=sleep, clock=clock)
    assert out.ok and 120 <= clock.t < 200


def test_timeout_when_no_file(arr, tmp_path):
    arr.jobs = None  # gammel Bazarr uden jobkø
    video = arr.video("F/F.mkv")
    clock = Clock()
    out = request_translation(
        Bazarr(make_cfg(tmp_path), arr.client()), cand_for(video), 45, sleep=clock.sleep, clock=clock
    )
    assert not out.ok and not out.permanent and "45 minutter" in out.reason
    assert clock.t >= 45 * 60


def test_existing_file_must_change(arr, tmp_path):
    video = arr.video("F/F.mkv")
    da = os.path.splitext(video)[0] + ".da.srt"
    with open(da, "w") as f:
        f.write(HAIKU)
    os.utime(da, (1_000_000, 1_000_000))
    clock = Clock()
    out = request_translation(
        Bazarr(make_cfg(tmp_path), arr.client()), cand_for(video), 5, sleep=clock.sleep, clock=clock
    )
    assert not out.ok


def test_failed_bazarr_job_stops_waiting_early(arr, tmp_path):
    video = arr.video("F/F.mkv")
    arr.on_translate = lambda form: arr.jobs.append(
        {"job_id": 9, "job_name": "Translating from EN to DA using Lingarr", "status": "failed"}
    )
    clock = Clock()
    out = request_translation(
        Bazarr(make_cfg(tmp_path), arr.client()), cand_for(video), 45, sleep=clock.sleep, clock=clock
    )
    assert not out.ok and "job fejlede" in out.reason and clock.t < 60


def test_blocking_bazarr_timeout_still_checks_file(tmp_path):
    video = tmp_path / "F.mkv"
    video.write_bytes(b"v")
    da = tmp_path / "F.da.srt"

    class SlowBazarr:
        def jobs(self):
            return None

        def translate(self, *a, **kw):
            da.write_text(HAIKU)
            raise httpx2.ReadTimeout("for langsom")

    clock = Clock()
    out = request_translation(SlowBazarr(), cand_for(str(video)), 45, sleep=clock.sleep, clock=clock)
    assert out.ok


# --- genstart midt i en oversættelse -------------------------------------------------
def test_resume_after_restart(arr, tmp_path):
    video, en_path = movie_with_english(arr)
    r, db = runner(arr, tmp_path, claude=FakeClaude([response([])]))
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
        f.write(HAIKU)
    r.resume()
    assert db.get("movie:1")["status"] == "faerdig"
    assert "Oversat af Claude" in open(da, encoding="utf-8").read()


def test_resume_without_file_counts_as_failed_attempt(arr, tmp_path):
    r, db = runner(arr, tmp_path)
    db.upsert(
        "movie:2",
        media_type="movie",
        media_id=2,
        title="Film 2",
        status="i_gang",
        attempts=1,
        da_path=str(tmp_path / "findes-ikke.da.srt"),
        started_at="2020-01-01T00:00:00+00:00",
    )
    r.resume()
    assert db.get("movie:2")["status"] == "forsoeg_igen"


# --- testtilstand ---------------------------------------------------------------------
def test_cli_test_mode(tmp_path, capsys):
    en, da, out = tmp_path / "en.srt", tmp_path / "da.srt", tmp_path / "ny.srt"
    en.write_text(EN, encoding="utf-8")
    da.write_text(HAIKU, encoding="utf-8")
    before = {p: p.read_bytes() for p in (en, da)}
    claude = FakeClaude([response([{"i": 2, "da": "Jeg har 3 søstre."}])])
    cfg = make_cfg(tmp_path)
    assert run_test(cfg, str(en), str(da), str(out), client=claude, budget=FakeBudget()) == 0
    assert {p: p.read_bytes() for p in (en, da)} == before
    assert "Jeg har 3 søstre." in out.read_text(encoding="utf-8")
    report = (tmp_path / "ny.srt.rapport.txt").read_text(encoding="utf-8")
    assert "Flag før korrektur: 1" in report and "Flag efter korrektur: 0" in report
    assert "Rettelser fra korrektur: 1" in report and "Pris: 0.0140 USD" in report
    assert "Korrekturmodel: claude-opus-5-5 (effort medium)" in report and "Sonnet" not in report
    assert "Kreditlinje: Bazarrs kreditlinje erstattet" in report
    assert "Testrapport" in capsys.readouterr().out
    # nægter at overskrive
    assert run_test(cfg, str(en), str(da), str(da), client=claude, budget=FakeBudget()) == 2
    assert run_test(cfg, str(en), str(da), str(out), client=claude, budget=FakeBudget()) == 2


def test_outcome_defaults():
    assert Outcome(True).permanent is False


# --- forklarende log i hver runde -----------------------------------------------------
def round_log(arr, tmp_path, caplog, **kw):
    r, _ = runner(arr, tmp_path, claude=FakeClaude([]), dry_run=True, **kw)
    caplog.clear()
    with caplog.at_level("DEBUG", logger="oversaetter"):
        cands = r.run_once()
    return cands, caplog.text


def test_round_log_empty_wanted_list(arr, tmp_path, caplog):
    cands, text = round_log(arr, tmp_path, caplog)
    assert cands == []
    assert "Bazarr melder ingen film eller afsnit, der mangler dansk. Tjek at dansk er med i sprogprofilen" in text


def test_round_log_raw_numbers_and_reasons(arr, tmp_path, caplog):
    # ville søge nu
    movie_with_english(arr)
    # ikke Seerr-ønske og intet mærke
    arr.add_movie(2, 102, {"days": 2}, [embedded("en", 5)], requested=False, title="Uønsket film")
    # gyldig søgning før - mangler stadig dansk -> ville oversætte
    arr.add_movie(3, 103, {"minutes": 37}, [embedded("en", 6)], title="Søgt film")
    # gyldig søgning før - ingen engelsk tekst
    arr.add_movie(4, 104, {"days": 2}, [embedded("sv", 7)], title="Svensk film")
    # gyldig søgning før - kun PGS
    pgs = os.path.join(arr.media_dir, "Film 5", "Film 5.en.sup")
    arr.add_movie(5, 105, {"days": 2}, [en_sub(pgs)], title="PGS-film")
    # afsnit uden mærke og uden Seerr-ønske
    arr.add_episode(6, 60, 6000, {"days": 9}, [], tags=[1])
    db = Database(os.path.join(make_cfg(tmp_path).data_dir, "oversaetter.db"))
    for i in (3, 4, 5):
        db.upsert(f"movie:{i}", media_type="movie", media_id=i, status="soegt", search_at="2026-10-17T10:00:00+00:00")
    cands, text = round_log(arr, tmp_path, caplog)
    assert [c.media_id for c in cands] == [3]
    assert "Bazarr melder 5 film og 1 afsnit på wanted-listen (heraf 5 film og 1 afsnit uden dansk)" in text
    examples = [line for line in text.splitlines() if "Eksempel:" in line]
    assert len(examples) == 5
    assert "Film 1 - ville søge nu (Seerr, ingen gyldig søgning endnu)" in text
    assert "Uønsket film - ikke Seerr-ønske og mangler mærket oversaet" in text
    assert "Søgt film - gyldig søgning fandt ikke dansk - oversætter (Seerr, indlejret engelsk spor" in text
    assert "Svensk film - ingen engelsk tekstkilde" in text
    assert "PGS-film - kun PGS/VobSub" in text
    assert "Serie 60 1x06 - ikke Seerr-ønske og mangler mærket oversaet" in text
    assert "... og 1 flere" in text
    assert "DRY_RUN: ville søge nu efter dansk til Film 1 (Seerr, ingen gyldig søgning endnu)" in text
    assert "DRY_RUN: ville oversætte nu: Søgt film (Seerr, gyldig søgning fandt ikke dansk)" in text
    assert arr.search_calls == [] and arr.translate_calls == []


def test_round_log_bazarr_wanted_without_danish(arr, tmp_path, caplog):
    movie_with_english(arr)
    r, _ = runner(arr, tmp_path, dry_run=True)
    orig = arr.bazarr

    def no_danish(req, path, q):
        resp = orig(req, path, q)
        if path == "/api/movies/wanted":
            data = resp.json()["data"]
            for d in data:
                d["missing_subtitles"] = [{"code2": "sv", "forced": False}]
            return httpx2.Response(200, json={"data": data})
        return resp

    arr.bazarr = no_danish
    with caplog.at_level("INFO", logger="oversaetter"):
        assert r.run_once() == []
    assert "Bazarr melder 1 film og 0 afsnit på wanted-listen (heraf 0 film og 0 afsnit uden dansk)" in caplog.text
    assert "Ingen af dem mangler dansk ifølge Bazarr. Tjek at dansk er med i sprogprofilen i Bazarr." in caplog.text


@pytest.mark.parametrize(
    "service, failure, expected",
    [
        ("bazarr", 401, "Runden er afbrudt: Bazarr svarede med HTTP 401 på /api/movies/wanted (forkert API-nøgle"),
        ("bazarr", "timeout", "Runden er afbrudt: Bazarr svarede ikke inden for 60 sekunder (timeout)"),
        ("seerr", 401, "Kunne ikke hente ønsker fra Seerr: Seerr svarede med HTTP 401 på /api/v1/request"),
        ("seerr", "timeout", "Seerr svarede ikke inden for 60 sekunder (timeout) på http://seerr/api/v1/request"),
        ("radarr", 401, "Radarr svarede med HTTP 401 på /api/v3/movie (forkert API-nøgle i .env)"),
        ("radarr", "timeout", "Radarr svarede ikke inden for 60 sekunder (timeout)"),
        ("sonarr", 404, "Sonarr svarede med HTTP 404 på /api/v3/series/60 (forkert adresse i .env"),
        ("sonarr", 500, "Sonarr svarede med HTTP 500 på /api/v3/series/60 (fejl i tjenesten)"),
    ],
)
def test_round_log_service_errors(arr, tmp_path, caplog, service, failure, expected):
    movie_with_english(arr)
    arr.add_movie(2, 102, {"days": 2}, [embedded("en", 5)])
    arr.add_episode(6, 60, 6000, {"days": 9}, [], tags=[7])
    arr.fail[service] = failure
    keys = dict(radarr_api_key="radarr-hemmelig", sonarr_api_key="sonarr-hemmelig", seerr_api_key="seerr-hemmelig")
    _, text = round_log(arr, tmp_path, caplog, **keys)
    assert expected in text
    for secret in ("bazarr-key", *keys.values()):
        assert secret not in text
    if service == "radarr":
        # samme fejl for flere film logges én gang og tælles
        assert text.count("Kunne ikke slå") == 1 and "Samme fejl ramte 2 emner" in text
