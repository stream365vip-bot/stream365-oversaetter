"""Forbedringerne fra 18.10.2026: filmkontekst, ordliste, prompt v4, skarpere kontrol og parallelle bidder.

Alle tekster er selvskrevne.
"""

import json
import logging
import os
import re
import threading
from types import SimpleNamespace

import httpx2
import pytest

from oversaetter import kontekst, ordliste, oversaet
from oversaetter.__main__ import main
from oversaetter.checks import check_cue
from oversaetter.clients import ApiError
from oversaetter.config import Config
from oversaetter.oversaet import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_V4,
    THINK_FIRST,
    build_system,
    run_translate_test,
    translate_all,
)
from oversaetter.selection import Candidate
from tests.fakes import FakeArr, en_sub
from tests.helpers import cfg, srt
from tests.test_direct import Clock, reply
from tests.test_direct import runner as direct_runner
from tests.test_oversaet import FakeProvider, request_payload, translations

GLOSSARY_REPLY = {
    "terms": [
        {"en": "Big Mike", "da": "Store Mike", "note": "øgenavn"},
        {"en": "Tom", "da": "Tom", "note": "navn"},
        {"en": "the Hollow", "da": "Hulen", "note": "sted"},
    ],
    "address": [{"who": "Tom", "to": "Big Mike", "form": "du"}],
    "tone": "rå komedie",
}


# --- falske servere og klienter ------------------------------------------------------------------------
class FakeTmdb:
    """Falsk TMDB (httpx2.MockTransport). fail=True giver HTTP 500 på alt."""

    def __init__(self, fail=False, da_overview="En dansk handling om en stor fyr."):
        self.fail = fail
        self.da_overview = da_overview
        self.requests: list[httpx2.Request] = []

    def handle(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        if self.fail:
            return httpx2.Response(500, json={})
        path, lang = req.url.path, req.url.params.get("language")
        credits = {
            "cast": [
                {"name": "Ann Actor", "character": "Sara", "gender": 1, "order": 0},
                {"name": "Bob Actor", "character": "Big Mike", "gender": 2, "order": 1},
            ]
        }
        if path == "/3/movie/603":
            return httpx2.Response(
                200,
                json={
                    "title": "Filmen",
                    "original_title": "The Film",
                    "release_date": "2024-05-01",
                    "genres": [{"name": "Komedie"}],
                    "original_language": "en",
                    "production_countries": [{"iso_3166_1": "US"}],
                    "overview": self.da_overview if lang == "da-DK" else "An English plot.",
                    "credits": credits,
                    "release_dates": {"results": [{"iso_3166_1": "DK", "release_dates": [{"certification": "15"}]}]},
                },
            )
        if path == "/3/tv/77":
            return httpx2.Response(200, json={"name": "Serien", "overview": "Dansk serie.", "credits": credits})
        if path == "/3/tv/77/season/1/episode/2":
            return httpx2.Response(200, json={"name": "Afsnittet", "overview": "Dansk afsnit."})
        return httpx2.Response(404, json={})

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle))


def tmdb(fake: FakeTmdb, key="v3-nøgle") -> kontekst.Tmdb:
    return kontekst.Tmdb("https://api.themoviedb.org/3", key, fake.client())


class SmartClaude:
    """Falsk Anthropic-klient, der kender ordlistekaldet, oversættelsen og rettelsesrunden fra hinanden."""

    def __init__(self, glossary=GLOSSARY_REPLY, translate=None):
        self.requests: list[dict] = []
        self.messages = self
        self.glossary = glossary
        self.translate = translate
        self.lock = threading.Lock()

    @staticmethod
    def system_text(kwargs) -> str:
        system = kwargs["system"]
        return system if isinstance(system, str) else "".join(b["text"] for b in system)

    def kind(self, kwargs) -> str:
        text = self.system_text(kwargs)
        if text.startswith(ordliste.PROMPT[:40]):
            return "ordliste"
        if text.startswith(SYSTEM_PROMPT[:60]):
            return "oversæt"
        return "ret"

    def create(self, **kwargs):
        with self.lock:
            self.requests.append(kwargs)
        kind = self.kind(kwargs)
        if kind == "ordliste":
            if isinstance(self.glossary, Exception):
                raise self.glossary
            return reply(json.dumps(self.glossary, ensure_ascii=False))
        if kind == "ret":
            return reply(json.dumps({"fixes": []}))
        if self.translate is not None:
            return reply(self.translate(kwargs))
        return reply(translations(kwargs))

    def of(self, kind: str) -> list[dict]:
        return [r for r in self.requests if self.kind(r) == kind]


def write_en(tmp_path, cues=None, name="film.en.srt") -> str:
    path = tmp_path / name
    path.write_text(
        srt(*(cues or [("00:00:01,000", "00:00:03,000", "Hello there."), ("00:00:04,000", "00:00:06,000", "Bye.")]))
    )
    return str(path)


# --- 1) filmkontekst -----------------------------------------------------------------------------------
def test_context_from_radarr():
    radarr = SimpleNamespace(
        movie=lambda rid: {
            "title": "Do Not Enter",
            "year": 2024,
            "genres": ["Horror", "Comedy"],
            "certification": "R",
            "originalLanguage": {"id": 1, "name": "English"},
            "overview": "Friends enter a haunted hotel.",
            "tmdbId": 603,
        }
    )
    cand = Candidate("movie", 1, "Do Not Enter", "Seerr")
    text = kontekst.for_candidate(cand, radarr, None, None)
    assert "Title: Do Not Enter (2024)" in text and "Genre: Horror, Comedy" in text
    assert "Age rating: R" in text and "English" in text and "Plot: Friends enter a haunted hotel." in text


def test_context_from_tmdb_has_danish_plot_and_cast_with_gender():
    fake = FakeTmdb()
    radarr = SimpleNamespace(movie=lambda rid: {"title": "The Film", "overview": "English plot.", "tmdbId": 603})
    text = kontekst.for_candidate(Candidate("movie", 1, "The Film", "Seerr"), radarr, None, tmdb(fake))
    assert "Plot: En dansk handling om en stor fyr." in text
    assert "- Ann Actor - Sara - female" in text and "- Bob Actor - Big Mike - male" in text
    assert "Age rating: 15" in text and "US" in text and "(2024)" in text
    req = fake.requests[0]
    assert req.url.params["language"] == "da-DK" and "credits" in req.url.params["append_to_response"]


def test_context_from_tmdb_without_danish_plot_falls_back_to_english():
    text = kontekst.for_tmdb_id(tmdb(FakeTmdb(da_overview="")), "603")
    assert "Plot: An English plot." in text and "Title: The Film (2024)" in text


def test_context_for_series_uses_series_and_episode_text():
    sonarr = SimpleNamespace(
        episode=lambda eid: {"seriesId": 50, "seasonNumber": 1, "episodeNumber": 2, "title": "Pilot 2"},
        series=lambda sid: {"title": "Serien", "overview": "Engelsk serie.", "tmdbId": 77},
    )
    cand = Candidate("episode", 5, "Serien 1x02", "mærke", series_id=50)
    text = kontekst.for_candidate(cand, None, sonarr, tmdb(FakeTmdb()))
    assert text.startswith("Series context")
    assert 'Season 1 episode 2: "Afsnittet"' in text
    assert "Series plot: Dansk serie." in text and "Episode plot: Dansk afsnit." in text


def test_tmdb_v4_token_is_sent_as_bearer():
    fake = FakeTmdb()
    kontekst.for_tmdb_id(tmdb(fake, key="eyJhbGciOi.token"), "603")
    req = fake.requests[0]
    assert req.headers["Authorization"] == "Bearer eyJhbGciOi.token" and "api_key" not in req.url.params


def test_failed_lookup_continues_without_context(caplog):
    def boom(rid):
        raise ApiError("Radarr svarede med HTTP 500")

    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        assert (
            kontekst.for_candidate(Candidate("movie", 1, "X", "Seerr"), SimpleNamespace(movie=boom), None, None) is None
        )
    assert "oversætter uden kontekst" in caplog.text


def test_failed_tmdb_keeps_radarr_context(caplog):
    radarr = SimpleNamespace(movie=lambda rid: {"title": "The Film", "overview": "English plot.", "tmdbId": 603})
    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        text = kontekst.for_candidate(Candidate("movie", 1, "X", "Seerr"), radarr, None, tmdb(FakeTmdb(fail=True)))
    assert "Plot: English plot." in text and "TMDB-opslag fejlede" in caplog.text
    assert "v3-nøgle" not in caplog.text  # nøglen kommer aldrig i loggen


# --- 2) ordliste ---------------------------------------------------------------------------------------
def test_glossary_is_parsed_capped_and_rendered():
    terms = [{"en": f"Term{i}", "da": f"Ord{i}", "note": ""} for i in range(70)]
    text = "```json\n" + json.dumps({"terms": terms + [{"en": "", "da": "x"}], "address": [], "tone": "t"}) + "\n```"
    wl = ordliste.parse(text)
    assert len(wl.terms) == 60 and wl.tone == "t"
    wl = ordliste.parse(json.dumps(GLOSSARY_REPLY))
    block = wl.block()
    assert '"Big Mike" -> "Store Mike" (øgenavn)' in block and "Tom to Big Mike: du" in block
    assert ordliste.Wordlist.from_json(wl.to_json()).terms == wl.terms


def test_glossary_input_is_capped(monkeypatch):
    monkeypatch.setattr(ordliste, "MAX_INPUT_TOKENS", 100)
    en = [f"Line {i} with some words." for i in range(300)]
    text = ordliste.english_text(en)
    assert len(text) <= 350 and text.startswith("Line 0")


def test_glossary_is_sent_in_every_batch_and_cached(tmp_path, capsys):
    en = write_en(tmp_path, [(f"00:00:{i:02d},000", f"00:00:{i:02d},900", f"Big Mike line {i}.") for i in range(1, 8)])
    fake = SmartClaude()
    out = str(tmp_path / "ud.srt")
    assert run_translate_test(cfg(), en, out, "anthropic", "claude-opus-5-5", batch=3, client=fake, glossary=True) == 0
    assert len(fake.of("ordliste")) == 1 and fake.requests[0] is fake.of("ordliste")[0]  # før bidderne
    batches = fake.of("oversæt")
    assert len(batches) == 3
    for req in batches:
        block = req["system"][0]
        assert '"Big Mike" -> "Store Mike"' in block["text"] and block["cache_control"] == {"type": "ephemeral"}
    assert "Big Mike line 1." in fake.of("ordliste")[0]["messages"][0]["content"]
    report = capsys.readouterr().out
    assert "Ordliste: 3 termer, 1 tiltaler, 0.0" in report


def test_glossary_failure_translates_without(tmp_path, capsys, caplog):
    en = write_en(tmp_path)
    fake = SmartClaude(glossary=RuntimeError("nede"))
    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        assert run_translate_test(cfg(), en, str(tmp_path / "u.srt"), "anthropic", "m", client=fake, glossary=True) == 0
    assert fake.of("oversæt")[0]["system"] == SYSTEM_PROMPT
    assert "oversætter uden ordliste" in caplog.text and "Ordliste: kunne ikke laves" in capsys.readouterr().out


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def episode(arr, ep_id):
    video = arr.add_episode(ep_id, 50, 500, {"days": 2}, [], tags=[7])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(srt(("00:00:10,000", "00:00:12,000", "Big Mike is here."), ("00:00:13,000", "00:00:15,000", "Run.")))
    arr.episodes[ep_id]["subtitles"] = [en_sub(en_path)]
    return video


def test_series_glossary_is_saved_and_reused(arr, tmp_path):
    episode(arr, 5)
    claude = SmartClaude()
    r, db = direct_runner(arr, tmp_path, claude=claude, glossary=True)
    r.run_once()
    row = db.get("episode:5")
    assert row["status"] == "faerdig" and row["series_id"] == 50
    assert json.loads(row["glossary"])["terms"][0] == {"en": "Big Mike", "da": "Store Mike", "note": "øgenavn"}
    # Næste afsnit i samme serie får den gemte ordliste med
    episode(arr, 6)
    claude.glossary = {"terms": [{"en": "Run", "da": "Løb", "note": ""}], "address": [], "tone": ""}
    r.run_once()
    second = claude.of("ordliste")[-1]["messages"][0]["content"]
    assert "previous_glossary" in second and "Store Mike" in second
    saved = json.loads(db.get("episode:6")["glossary"])
    assert [t["en"] for t in saved["terms"]] == ["Run", "Big Mike", "Tom", "the Hollow"]
    assert '"Big Mike" -> "Store Mike"' in claude.of("oversæt")[-1]["system"][0]["text"]
    # Ordlistekaldet tæller med i budgettet
    assert db.conn.execute("SELECT COUNT(*) FROM spend WHERE item_key = 'episode:6'").fetchone()[0] >= 2


def test_drift_with_context_on_and_failing_tmdb_still_translates(arr, tmp_path, caplog):
    episode(arr, 5)
    arr.series[50].update(tmdbId=77, title="Serie 50")
    key = tmp_path / "tmdb"
    key.write_text("nøgle")
    claude = SmartClaude()
    r, db = direct_runner(arr, tmp_path, claude=claude, context=True, tmdb_api_key_file=str(key))
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()  # TMDB svarer 404 fra den falske server
    assert db.get("episode:5")["status"] == "faerdig"
    assert "TMDB-opslag fejlede" in caplog.text and "nøgle" not in caplog.text.replace("Ingen", "")
    assert "Series context" in claude.of("oversæt")[0]["system"][0]["text"]


def test_drift_with_failing_context_translates_without(arr, tmp_path, caplog):
    episode(arr, 5)
    claude = SmartClaude()
    r, db = direct_runner(arr, tmp_path, claude=claude, context=True)

    def sonarr_down(ident):
        raise ApiError("Sonarr svarede med HTTP 500")

    r.sonarr.episode = r.sonarr.series = sonarr_down
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.process_direct(
            Candidate(
                "episode",
                5,
                "Serie 50 1x05",
                "mærke",
                arr.episodes[5]["path"],
                series_id=50,
                source_path=os.path.splitext(arr.episodes[5]["path"])[0] + ".en.srt",
            )
        )
    assert db.get("episode:5")["status"] == "faerdig"
    assert claude.of("oversæt")[0]["system"] == SYSTEM_PROMPT
    assert "oversætter uden kontekst" in caplog.text


def test_drift_context_from_sonarr_is_in_system(arr, tmp_path):
    episode(arr, 5)
    arr.series[50]["overview"] = "A series about friends."
    claude = SmartClaude()
    r, _ = direct_runner(arr, tmp_path, claude=claude, context=True, tmdb_api_key_file=str(tmp_path / "ingen"))
    r.run_once()
    system = claude.of("oversæt")[0]["system"][0]["text"]
    assert "Series context" in system and "Series plot: A series about friends." in system


# --- 3) prompt v4 --------------------------------------------------------------------------------------
def test_v4_is_built_with_priorities_and_examples():
    v4 = build_system("v4", "anthropic")
    assert v4 == SYSTEM_PROMPT_V4 and v4 != SYSTEM_PROMPT
    for part in (
        "1) the meaning is right, 2) at most 2 lines",
        '"You never know." -> "Du ved aldrig." Right: "Man ved aldrig."',
        '"bilnøgle", "hovednøgle", "nattesynsbriller"',
        "Lower case for weekdays, months, nationalities and languages",
        '"I mean" -> "altså"',
        '"please" (begging) -> "kom nu"/"vær sød"',
        'gets an empty "da": ""',
        "Other foreign languages in the source",
        "Song lyrics and signs are translated",
        "film glossary",
    ):
        assert part in v4
    rules = [line for line in v4.split("\n") if re.match(r"\d+\. ", line)]
    assert len(rules) == 17 and all("Wrong" in r or "->" in r for r in rules[:16])


@pytest.mark.parametrize("provider,think", [("anthropic", False), ("deepseek", True), ("gemini", True)])
def test_think_first_only_for_deepseek_and_gemini(provider, think):
    assert (THINK_FIRST in build_system("v4", provider)) == think
    assert THINK_FIRST not in build_system("v3", provider)


def test_v3_without_switches_is_unchanged():
    assert build_system("v3", "anthropic") == SYSTEM_PROMPT
    wl = ordliste.parse(json.dumps(GLOSSARY_REPLY))
    full = build_system("v3", "anthropic", "Film context: X", wl)
    assert full.startswith(SYSTEM_PROMPT) and "Film context: X" in full and "Store Mike" in full


def test_empty_cues_with_only_filler_are_removed_from_file(tmp_path, capsys):
    en = write_en(
        tmp_path,
        [
            ("00:00:01,000", "00:00:03,000", "Hello there."),
            ("00:00:04,000", "00:00:05,000", "Uh... hmm."),
            ("00:00:06,000", "00:00:08,000", "Bye."),
        ],
    )

    def answer(kwargs):
        lines = [
            {"i": x["i"], "da": "" if x["i"] == 2 else f"Dansk {x['i']}"} for x in request_payload(kwargs)["lines"]
        ]
        return json.dumps({"lines": lines})

    fake = FakeProvider("anthropic", answer)
    out = tmp_path / "ud.srt"
    assert run_translate_test(cfg(), en, str(out), "anthropic", "m", client=fake, prompt="v4") == 0
    text = out.read_text()
    assert "Dansk 1" in text and "Dansk 3" in text and "00:00:04" not in text and text.count("-->") == 2
    assert "heraf 1 med kun fyldlyde fjernet" in capsys.readouterr().out
    assert len(fake.requests) == 1  # tomt svar på fyldlyde er gyldigt i v4 (ingen ny runde)


def test_empty_answer_for_real_line_is_still_an_error_in_v4():
    def answer(kwargs):
        return json.dumps({"lines": [{"i": x["i"], "da": ""} for x in request_payload(kwargs)["lines"]]})

    fake = FakeProvider("anthropic", answer)
    en = ["Hello there."]
    res = translate_all(en, "anthropic", fake, "m", None, 50, 60.0, empty_ok=oversaet.filler_positions(en, "v4"))
    assert res.fallback == [0] and len(fake.requests) == 2


def test_drift_v4_removes_filler_and_uses_strict_checks(arr, tmp_path):
    video = arr.add_movie(1, 101, {"days": 2}, [])
    en_path = os.path.splitext(video)[0] + ".en.srt"
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(
            srt(
                ("00:00:10,000", "00:00:12,000", "Where are the car keys?"),
                ("00:00:13,000", "00:00:14,000", "Um..."),
                ("00:00:15,000", "00:00:17,000", "You never know."),
            )
        )
    arr.movies[1]["subtitles"] = [en_sub(en_path)]
    danish = {"Where are the car keys?": "Hvor er bil nøglerne?", "Um...": "", "You never know.": "Du ved aldrig."}

    def translate(kwargs):
        lines = [{"i": x["i"], "da": danish[x["en"]]} for x in request_payload(kwargs)["lines"]]
        return json.dumps({"lines": lines}, ensure_ascii=False)

    claude = SmartClaude(translate=translate)
    r, db = direct_runner(arr, tmp_path, claude=claude, prompt_version="v4")
    r.run_once()
    assert claude.of("oversæt")[0]["system"][0]["text"] == SYSTEM_PROMPT_V4
    fix = request_payload(claude.of("ret")[0])["lines"]
    flagged = {x["i"]: x["flag"] for x in fix if x.get("fix")}
    assert set(flagged) == {1, 2}  # positioner efter at fyldlyden er fjernet
    text = open(os.path.splitext(video)[0] + ".da.srt", encoding="utf-8").read()
    assert "00:00:13,000" not in text
    assert db.get("movie:1")["flags_before"] == 2


# --- 4) skarpere kontrol -------------------------------------------------------------------------------
def flags(da, en="", duration_ms=2000, glossary=None):
    return check_cue(da, en, 42, None, duration_ms, glossary, strict=True)


@pytest.mark.parametrize(
    "flag,positive,negative",
    [
        ("compound_space", ("Hvor er bil nøglen?", "Where's the car key?"), ("Hvor er bilnøglen?", "Where's the key?")),
        ("generic_you", ("Du ved aldrig.", "You never know."), ("Man ved aldrig.", "You never know.")),
        ("capitalized_weekday", ("Vi ses på Mandag.", "See you Monday."), ("Mandag passer.", "Monday works.")),
        ("english_word", ("Det er the end.", "That is the end."), ("Sorry, det er slut.", "Sorry, it's over.")),
        (
            "inconsistent_term",
            ("Big Mike kommer.", "Big Mike is coming."),
            ("Store Mike kommer.", "Big Mike is coming."),
        ),
    ],
)
def test_new_flags_positive_and_negative(flag, positive, negative):
    wl = ordliste.parse(json.dumps(GLOSSARY_REPLY))
    assert flag in flags(*positive, glossary=wl)
    assert flag not in flags(*negative, glossary=wl)


def test_generic_you_not_flagged_when_addressed():
    assert "generic_you" not in flags("Du ved aldrig, Tom.", "You never know, Tom.")
    assert "generic_you" not in flags("Du skal bare gå, mand.", "You just go, man.")
    assert "generic_you" not in flags("Du ved aldrig.", "Tom, you never know.")


def test_reading_speed():
    assert "reading_speed" in flags("Det her er en meget lang dansk replik", "Too fast.", duration_ms=1000)
    assert "reading_speed" not in flags("Det her er en lang replik", "Fine.", duration_ms=3000)
    # Den engelske replik er lige så hurtig: ikke oversætterens fejl
    assert "reading_speed" not in flags("Kort.", "This one is far longer.", duration_ms=200)


def test_new_flags_only_when_strict():
    assert check_cue("Hvor er bil nøglen?", "Where is the car key?", 42) == []


@pytest.mark.parametrize(
    "text",
    ["Big Mike", "Tom!", "1984", "12:30", "Stop!", "Okay.", "Merci, monsieur.", "¿Qué pasa, amigo?", "Danke schön."],
)
def test_identical_to_english_not_flagged_for_names_numbers_and_foreign(text):
    wl = ordliste.parse(json.dumps(GLOSSARY_REPLY))
    assert "identical_to_english" not in check_cue(text, text, 42, None, 2000, wl)


def test_identical_to_english_still_flags_english():
    assert "identical_to_english" in check_cue("Come with me now", "Come with me now", 42)


# --- 5) kontakter --------------------------------------------------------------------------------------
def test_switches_default_off_and_from_env():
    c = Config.from_env({})
    assert (c.prompt_version, c.context, c.glossary, c.parallel) == ("v3", False, False, 1)
    c = Config.from_env({"PROMPT_VERSION": "V4", "CONTEXT": "on", "GLOSSARY": "on", "PARALLEL": "4"})
    assert (c.prompt_version, c.context, c.glossary, c.parallel) == ("v4", True, True, 4)
    assert c.switches() == "prompt v4, kontekst on, ordliste on, parallel 4"
    with pytest.raises(ValueError, match="PROMPT_VERSION"):
        Config.from_env({"PROMPT_VERSION": "v5"})


def test_tmdb_key_from_env_or_file(tmp_path):
    key = tmp_path / "tmdb"
    key.write_text("fil-nøgle\n")
    assert Config.from_env({"TMDB_API_KEY_FILE": str(key)}).tmdb_key() == "fil-nøgle"
    assert Config.from_env({"TMDB_API_KEY_FILE": str(key), "TMDB_API_KEY": "env"}).tmdb_key() == "env"


def test_compose_mounts_tmdb_key_read_only():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    compose = open(os.path.join(root, "compose.yaml"), encoding="utf-8").read()
    assert "/opt/oversaetter/secrets/tmdb_api_key.txt:/run/secrets/tmdb_api_key:ro" in compose


def test_translate_test_with_all_switches(tmp_path, capsys):
    key = tmp_path / "tmdb"
    key.write_text("nøgle")
    en = write_en(
        tmp_path,
        [(f"00:00:{i:02d},000", f"00:00:{i:02d},900", f"Big Mike line {i}.") for i in range(1, 10)]
        + [("00:00:20,000", "00:00:21,000", "Hmm.")],
    )
    fake = SmartClaude(
        translate=lambda kw: json.dumps(
            {
                "lines": [
                    {"i": x["i"], "da": "" if x["en"] == "Hmm." else f"Dansk {x['i']}"}
                    for x in request_payload(kw)["lines"]
                ]
            }
        )
    )
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
    for part in (
        "Kontakter: prompt v4, kontekst on, ordliste on, parallel 4",
        "Filmkontekst: ",
        "Ordliste: 3 termer",
        "heraf 1 med kun fyldlyde fjernet",
        "Tid: ",
        "Pris: ",
    ):
        assert part in report
    assert "En dansk handling" in fake.of("ordliste")[0]["messages"][0]["content"]
    for req in fake.of("oversæt"):
        text = req["system"][0]["text"]
        assert text.startswith(SYSTEM_PROMPT_V4) and "Ann Actor - Sara - female" in text and "Store Mike" in text
    assert out.read_text().count("-->") == 9


def test_cli_passes_switches(tmp_path, monkeypatch, capsys):
    en = write_en(tmp_path)
    fake = SmartClaude()
    monkeypatch.setattr(oversaet, "make_client", lambda provider, c: fake)
    monkeypatch.setenv("TMDB_API_KEY_FILE", str(tmp_path / "ingen"))
    argv = ["oversaet-test", "--en", en, "--out", str(tmp_path / "u.srt"), "--provider", "anthropic"]
    argv += ["--model", "m", "--prompt", "v4", "--context", "on", "--glossary", "on", "--tmdb-id", "603"]
    argv += ["--parallel", "2"]
    assert main(argv) == 0
    report = capsys.readouterr().out
    assert "prompt v4, kontekst on, ordliste on, parallel 2" in report and "Filmkontekst: ingen" in report


# --- 6) parallelle bidder ------------------------------------------------------------------------------
def english(n):
    return [f"we talk about thing {i + 1}." for i in range(n)]


@pytest.mark.parametrize("provider", ["anthropic", "deepseek"])
def test_parallel_gives_same_lines_in_same_order(provider):
    one = translate_all(english(230), provider, FakeProvider(provider, translations), "m", None, 20, 60.0)
    fake = FakeProvider(provider, translations)
    four = translate_all(english(230), provider, fake, "m", None, 20, 60.0, parallel=4)
    assert four.da == one.da == [f"Dansk {i + 1}" for i in range(230)]
    assert four.batches == one.batches == 12 and four.calls == 12 and four.fallback == []
    assert four.input_tokens == one.input_tokens


def test_parallel_really_runs_at_the_same_time():
    running, peak, lock = [0], [0], threading.Lock()
    release = threading.Event()

    def answer(kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            if running[0] >= 3:
                release.set()
        release.wait(2)
        with lock:
            running[0] -= 1
        return translations(kwargs)

    res = translate_all(english(30), "anthropic", FakeProvider("anthropic", answer), "m", None, 10, 60.0, parallel=3)
    assert peak[0] == 3 and res.fallback == []


def test_error_in_one_parallel_batch_does_not_stop_the_others(caplog):
    def answer(kwargs):
        if request_payload(kwargs)["lines"][0]["i"] == 21:
            raise RuntimeError("nede")
        return translations(kwargs)

    fake = FakeProvider("anthropic", answer)
    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        res = translate_all(english(60), "anthropic", fake, "m", None, 10, 60.0, parallel=4)
    en = english(60)
    assert res.da[:20] == [f"Dansk {i + 1}" for i in range(20)] and res.da[30:] == [
        f"Dansk {i + 1}" for i in range(30, 60)
    ]
    assert res.da[20:25] == en[20:25]  # første halvdel af bid 3 fejler hver gang
    assert set(range(20, 25)) <= set(res.fallback) and not set(range(30, 60)) & set(res.fallback)


def test_parallel_omits_unfinished_danish_context():
    seen = []
    first_done = threading.Event()

    def answer(kwargs):
        payload = request_payload(kwargs)
        if payload["lines"][0]["i"] == 1:
            first_done.wait(0.5)  # bid 1 bliver færdig sidst
        seen.append(payload)
        return translations(kwargs)

    res = translate_all(english(20), "anthropic", FakeProvider("anthropic", answer), "m", None, 10, 60.0, parallel=2)
    second = next(p for p in seen if p["lines"][0]["i"] == 11)
    assert second["context_before"] == []  # de danske linjer var ikke færdige endnu
    assert [x["i"] for x in second["context_after"]] == []
    assert res.fallback == []


def test_parallel_respects_deadline():
    clock = Clock()

    def answer(kwargs):
        clock.t += 100
        return translations(kwargs)

    fake = FakeProvider("anthropic", answer)
    res = translate_all(english(50), "anthropic", fake, "m", None, 10, 60.0, deadline=150, clock=clock, parallel=1)
    assert res.timed_out and len(fake.requests) == 2
    fake = FakeProvider("anthropic", answer)
    clock.t = 0
    res = translate_all(english(50), "anthropic", fake, "m", None, 10, 60.0, deadline=150, clock=clock, parallel=2)
    assert res.timed_out and len(fake.requests) < 5 and len(res.da) == 50
