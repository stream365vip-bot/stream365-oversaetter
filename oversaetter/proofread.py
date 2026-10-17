"""Korrektur B: korrekturmodellen (PROOFREAD_MODEL) retter reelle fejl i den danske oversættelse."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from .checks import FLAG_TEXT, EnglishIndex, has_english_note, plain
from .config import Config
from .srt import Cue

log = logging.getLogger("oversaetter")

SYSTEM_PROMPT = """You are a senior editor of Danish subtitles for films and TV series. You receive a Danish \
subtitle translation that was machine-translated from English, in batches of numbered lines. Each line has \
"i" (line number), "en" (the English source), "da" (the current Danish text) and "flag" (problems found by an \
automatic check). Lines with "context": true belong to the previous batch and are only there for context: \
never return fixes for them.

Your job is to correct only real errors and leave good lines untouched:
- wrong meaning, missing or reversed negations, wrong numbers, wrong or misspelled names
- Danish spelling and grammar mistakes
- Norwegian, Swedish or German words or spellings (e.g. "uansett", "ikkje", "bara", "inte", letters ö/ä/ü)
- English words that are neither names nor common Danish loanwords. Allowed loanwords include okay, hey, \
wow, yo, cool, nice, shit, fuck and bro. Other English words such as please, prank and sweetheart must be \
translated into Danish.
- "beast" (and the wrong Danish forms "bestie"/"bestien") means "uhyre" in Danish, e.g. "menneskebestien" \
becomes "menneskeuhyret". Never use "bestie" or "bestien".
- English notes, reasoning or comments left by the translator (e.g. "Wait", "Hmm, let's see", "translation", \
"output", "Line 1", character counts). Remove them so only the Danish subtitle remains.
- literal word-for-word phrasing, idioms and slang that do not make sense in Danish: rewrite them so they \
sound natural to a Danish viewer while keeping the meaning
- inconsistent translations of the same word, name or term within the film (see "earlier_fixes" for \
decisions made in earlier batches and follow them)
- lines longer than {max_chars} characters or more than 2 lines: shorten or rebalance into at most 2 lines \
of at most {max_chars} characters each
- two speakers in one subtitle: each speaker's line starts with "-" on its own line, like "-Kom nu.\\n-Nej."

Rules:
- Keep swearing at the same strength as the English. Do not soften or strengthen it.
- Keep formatting tags such as <i>...</i> where they are in the current text.
- Use "\\n" for a line break inside a subtitle. Never more than 2 lines.
- Every line that has one or more flags MUST be included in your fixes with a corrected Danish text.
- Do not change lines that are already good. Do not make stylistic rewrites of correct lines.
- Never write explanations, notes or English text inside "da". Return only the finished Danish subtitle.

Answer only with JSON of the form {{"fixes": [{{"i": <line number>, "da": "<corrected Danish text>"}}]}}. \
If nothing needs fixing, answer {{"fixes": []}}."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "da": {"type": "string"}},
                "required": ["i", "da"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fixes"],
    "additionalProperties": False,
}

MAX_TOKENS = 16000


class Budget(Protocol):
    def spent(self) -> float: ...
    def record(self, item_key: str | None, input_tokens: int, output_tokens: int, cost: float) -> None: ...


class DbBudget:
    def __init__(self, db):
        self.db = db

    def spent(self) -> float:
        return self.db.month_spend()

    def record(self, item_key, input_tokens, output_tokens, cost) -> None:
        self.db.add_spend(item_key, input_tokens, output_tokens, cost)


@dataclass
class ProofreadResult:
    fixes: dict[int, str] = field(default_factory=dict)  # position (0-baseret) -> ny tekst
    rejected: int = 0
    cost: float = 0.0
    batches_ok: int = 0
    batches_failed: int = 0
    budget_stopped: bool = False


def validate_fix(fix, allowed: set[int], en_by_i: dict[int, str], max_lines: int = 2) -> tuple[bool, str]:
    if not isinstance(fix, dict):
        return False, "ikke et objekt"
    i, da = fix.get("i"), fix.get("da")
    if not isinstance(i, int) or isinstance(i, bool) or i not in allowed:
        return False, f"ukendt replik-nummer {i!r}"
    if not isinstance(da, str) or not plain(da).strip():
        return False, f"replik {i}: tom tekst"
    da = da.replace("\r\n", "\n").strip()
    if len(da.split("\n")) > max_lines:
        return False, f"replik {i}: mere end {max_lines} linjer"
    if len(da) > 250:
        return False, f"replik {i}: urimeligt lang"
    if has_english_note(da, en_by_i.get(i, "")):
        return False, f"replik {i}: indeholder engelske noter"
    return True, ""


def _usage_numbers(usage) -> tuple[int, int, int, int]:
    def get(name):
        return int(getattr(usage, name, 0) or 0)

    return (
        get("input_tokens"),
        get("output_tokens"),
        get("cache_read_input_tokens"),
        get("cache_creation_input_tokens"),
    )


def _batches(n: int, size: int, overlap: int):
    start = 0
    while start < n:
        end = min(n, start + size)
        yield max(0, start - overlap), start, end
        start = end


def proofread(
    cues: list[Cue],
    en_index: EnglishIndex,
    flags: dict[int, list[str]],
    cfg: Config,
    client,
    budget: Budget,
    item_key: str | None = None,
) -> ProofreadResult:
    result = ProofreadResult()
    system = SYSTEM_PROMPT.format(max_chars=min(40, cfg.max_line_chars))
    earlier: list[dict] = []
    en_text = [en_index.text_for(c) for c in cues]

    for ctx_start, start, end in _batches(len(cues), max(1, cfg.proofread_batch), cfg.proofread_overlap):
        if budget.spent() >= cfg.monthly_budget_usd:
            result.budget_stopped = True
            log.warning(
                "Månedens budget på %.2f USD er nået (brugt %.2f USD) - resten af korrekturen springes over",
                cfg.monthly_budget_usd,
                budget.spent(),
            )
            break
        lines = []
        for pos in range(ctx_start, end):
            text = result.fixes.get(pos, cues[pos].text)
            flag = [FLAG_TEXT.get(f, f) for f in flags.get(pos, [])]
            item = {"i": pos + 1, "en": en_text[pos], "da": text, "flag": flag}
            if pos < start:
                item["context"] = True
            lines.append(item)
        payload = {"earlier_fixes": earlier[-30:], "lines": lines}
        user = "Proofread these Danish subtitle lines and return the fixes as JSON.\n\n" + json.dumps(
            payload, ensure_ascii=False
        )
        allowed = set(range(start + 1, end + 1))
        en_by_i = {pos + 1: en_text[pos] for pos in range(start, end)}
        try:
            response = client.messages.create(
                model=cfg.proofread_model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": cfg.proofread_effort,
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
            )
        except Exception as e:  # netværk, API-fejl osv.
            result.batches_failed += 1
            log.error("Korrektur af replik %d-%d fejlede (%s) - bidden springes over", start + 1, end, e)
            continue

        inp, out, cache_read, cache_write = _usage_numbers(getattr(response, "usage", None))
        cost = cfg.price(inp, out, cache_read, cache_write)
        result.cost += cost
        budget.record(item_key, inp + cache_read + cache_write, out, cost)

        stop = getattr(response, "stop_reason", None)
        text = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), "")
        try:
            if stop in ("refusal", "max_tokens"):
                raise ValueError(f"stop_reason={stop}")
            data = json.loads(text)
            fixes = data["fixes"]
            if not isinstance(fixes, list):
                raise ValueError("fixes er ikke en liste")
        except (ValueError, KeyError, TypeError) as e:
            result.batches_failed += 1
            log.error("Ugyldigt svar fra korrekturmodellen for replik %d-%d (%s) - bidden kasseres", start + 1, end, e)
            continue

        result.batches_ok += 1
        for fix in fixes:
            ok, why = validate_fix(fix, allowed, en_by_i)
            if not ok:
                result.rejected += 1
                log.warning("Rettelse kasseret: %s", why)
                continue
            pos = fix["i"] - 1
            new = fix["da"].replace("\r\n", "\n").strip()
            if new != cues[pos].text:
                result.fixes[pos] = new
                earlier.append({"en": en_text[pos], "da": new})
    return result
