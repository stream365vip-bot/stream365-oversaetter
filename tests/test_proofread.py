import json

import anthropic
import httpx2

from oversaetter.checks import EnglishIndex
from oversaetter.postprocess import postprocess
from oversaetter.proofread import proofread, validate_fix
from tests.helpers import FakeBudget, FakeClaude, cfg, cue, response


def make(n):
    en = [cue(i * 3 + 10, i * 3 + 12, f"Line {i} in English with {i} apples.") for i in range(n)]
    da = [cue(i * 3 + 10, i * 3 + 12, f"Replik {i} på dansk med {i} æbler.") for i in range(n)]
    return en, da


def test_batches_with_overlap_and_applies_fixes():
    en, da = make(120)
    claude = FakeClaude(
        [response([{"i": 3, "da": "Rettet tre"}]), response([]), response([{"i": 120, "da": "Sidste"}])]
    )
    budget = FakeBudget()
    res = proofread(da, EnglishIndex(en), {}, cfg(proofread_batch=50), claude, budget)
    assert len(claude.requests) == 3
    first, second, third = claude.lines(0), claude.lines(1), claude.lines(2)
    assert [x["i"] for x in first] == list(range(1, 51))
    assert [x["i"] for x in second][:5] == [46, 47, 48, 49, 50] and all(x.get("context") for x in second[:5])
    assert second[5]["i"] == 51 and "context" not in second[5]
    assert third[-1]["i"] == 120
    assert set(first[0]) == {"i", "en", "da", "flag"}
    assert res.fixes == {2: "Rettet tre", 119: "Sidste"}
    # pris: 1000 input * 4 USD/M + 500 output * 20 USD/M = 0,014 pr. kald
    assert abs(res.cost - 3 * 0.014) < 1e-9
    assert budget.calls == 3
    req = claude.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["format"]["type"] == "json_schema"


def test_flags_are_sent():
    en, da = make(2)
    claude = FakeClaude([response([])])
    proofread(da, EnglishIndex(en), {1: ["line_too_long"]}, cfg(), claude, FakeBudget())
    assert claude.lines(0)[1]["flag"] == ["a line is too long"]


def test_invalid_json_is_handled():
    en, da = make(3)
    claude = FakeClaude([response(text="Here are the fixes: {not json")])
    res = proofread(da, EnglishIndex(en), {}, cfg(), claude, FakeBudget())
    assert res.fixes == {} and res.batches_failed == 1
    assert res.cost > 0  # betalte tokens tælles stadig


def test_wrong_shape_truncated_and_api_error_are_handled():
    en, da = make(3)
    for reply in [
        response(text='{"rettelser": []}'),
        response([{"i": 1, "da": "x"}], stop="max_tokens"),
        response(text="null"),
        RuntimeError("forbindelse tabt"),
    ]:
        res = proofread(da, EnglishIndex(en), {}, cfg(), FakeClaude([reply]), FakeBudget())
        assert res.fixes == {} and res.batches_failed == 1


def test_invalid_fixes_are_rejected():
    en, da = make(60)
    fixes = [
        {"i": 999, "da": "Ukendt nummer"},
        {"i": 2, "da": "En\nTo\nTre"},
        {"i": 3, "da": "Wait, the translation should be: Hej"},
        {"i": 4, "da": "   "},
        {"i": "5", "da": "Tekst"},
        {"i": 6, "da": "God rettelse"},
    ]
    claude = FakeClaude([response(fixes), response([{"i": 48, "da": "Kontekst må ikke rettes"}])])
    res = proofread(da, EnglishIndex(en), {}, cfg(proofread_batch=50), claude, FakeBudget())
    assert res.fixes == {5: "God rettelse"}
    assert res.rejected == 6


def test_validate_fix():
    assert validate_fix({"i": 1, "da": "Fint"}, {1}, {})[0]
    assert not validate_fix({"i": True, "da": "x"}, {1}, {})[0]
    assert not validate_fix("tekst", {1}, {})[0]


def test_budget_stops_before_next_batch():
    en, da = make(100)
    claude = FakeClaude([response([], inp=1_000_000, out=0), response([])])  # første kald koster 2 USD
    res = proofread(da, EnglishIndex(en), {}, cfg(monthly_budget_usd=1.0), claude, FakeBudget())
    assert len(claude.requests) == 1
    assert res.budget_stopped


def test_postprocess_skips_proofread_when_budget_is_reached():
    en = [cue(10, 12, "Hello there, my friend.")]
    da = [cue(1, 5, "# Subtitles translated with Lingarr #"), cue(10, 12, "Hej med dig, min ven.")]
    claude = FakeClaude([])
    res = postprocess(da, en, cfg(monthly_budget_usd=5.0), claude, FakeBudget(spent=5.0))
    assert claude.requests == []
    assert res.proofread == "budget nået"
    assert [c.text for c in res.cues] == ["Oversat af Claude for ≋", "Hej med dig, min ven."]


def test_postprocess_reports_flags_before_and_after():
    en = [cue(10, 12, "Where are you going?"), cue(13, 15, "I have 3 kids.")]
    da = [cue(10, 12, "Hvor skal du hen?"), cue(13, 15, "Jeg har tre ikkje børn.")]
    claude = FakeClaude([response([{"i": 2, "da": "Jeg har 3 børn."}])])
    res = postprocess(da, en, cfg(), claude, FakeBudget())
    assert res.flags_before == 1 and res.flags_after == 0 and res.fixes == 1
    assert res.cues[-1].text == "Jeg har 3 børn."


def test_with_real_sdk_and_fake_http():
    """Korrektur-kaldet gennem den rigtige anthropic-SDK, men med et falsk HTTP-svar."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps({"fixes": [{"i": 1, "da": "Hej, min ven."}]})}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 2000, "output_tokens": 1000},
            },
        )

    client = anthropic.Anthropic(
        api_key="test-nøgle", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    en = [cue(10, 12, "Hi, my friend.")]
    da = [cue(10, 12, "Hi, min ven.")]
    res = proofread(da, EnglishIndex(en), {}, cfg(), client, FakeBudget())
    assert res.fixes == {0: "Hej, min ven."}
    assert abs(res.cost - (2000 * 4 + 1000 * 20) / 1_000_000) < 1e-12
    assert seen["key"] == "test-nøgle"
    assert seen["body"]["model"] == "claude-opus-5-5"
    assert "tool_choice" not in seen["body"]


def test_known_bad_words_go_to_proofreading_with_instructions():
    en = [cue(10, 12, "The human beast is loose."), cue(13, 15, "Please, sweetheart.")]
    da = [cue(10, 12, "Menneskebestien er løs."), cue(13, 15, "Please, sweetheart.")]
    claude = FakeClaude([response([{"i": 1, "da": "Menneskeuhyret er løs."}, {"i": 2, "da": "Vær nu sød, skat."}])])
    res = postprocess(da, en, cfg(known_bad_words="darling"), claude, FakeBudget())
    sent = claude.lines()
    assert all(any("known wrong word" in f for f in line["flag"]) for line in sent)
    system = claude.requests[0]["system"]
    assert '"uhyre"' in system and "menneskeuhyret" in system
    assert "please, prank and sweetheart must be translated" in system
    assert res.flags_before == 2 and res.flags_after == 0 and res.fixes == 2
    assert res.model == "claude-opus-5-5" and "rettelser fra korrektur: 2" in res.summary()
    assert "korrekturmodel: claude-opus-5-5" in res.summary()
