"""Efterbehandling: kontrol (A), korrektur (B) og kreditlinje (C)."""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, replace

from .checks import EnglishIndex, check_all
from .config import Config
from .credit import apply_credit, split_header
from .proofread import Budget, ProofreadResult, proofread
from .srt import Cue

log = logging.getLogger("oversaetter")


@dataclass
class PostResult:
    cues: list[Cue]
    flags_before: int
    flags_after: int
    fixes: int
    rejected: int
    cost: float
    proofread: str  # "udført", "budget nået", "ingen nøgle", "slået fra"
    credit: str
    remaining: dict[int, list[str]]
    model: str = ""

    def summary(self) -> str:
        return (
            f"flag før: {self.flags_before}, flag efter: {self.flags_after}, rettelser fra korrektur: {self.fixes}, "
            f"korrekturmodel: {self.model}, "
            f"kasserede rettelser: {self.rejected}, pris: {self.cost:.4f} USD, korrektur: {self.proofread}, "
            f"{self.credit}"
        )


def _flag_summary(flags: dict[int, list[str]]) -> str:
    counts = Counter(f for fl in flags.values() for f in fl)
    return ", ".join(f"{k}={v}" for k, v in counts.most_common()) or "ingen"


def postprocess(
    da_cues: list[Cue],
    en_cues: list[Cue],
    cfg: Config,
    client,
    budget: Budget,
    item_key: str | None = None,
    clock=time.monotonic,
) -> PostResult:
    """Kreditlinjen sættes altid ind - også når korrekturen fejler, afvises eller stoppes af tidsgrænsen."""
    deadline = clock() + cfg.postprocess_timeout_minutes * 60
    body, had_header = split_header(da_cues, cfg.credit_text)
    en_body, _ = split_header(en_cues)
    en_index = EnglishIndex(en_body)

    bad_words = cfg.bad_words()
    before = check_all(body, en_index, cfg.max_line_chars, bad_words)
    log.info("Kontrol før korrektur: %d replikker flaget (%s)", len(before), _flag_summary(before))

    pr = ProofreadResult()
    if client is None:
        status = "ingen API-nøgle"
        log.warning("Korrektur springes over: %s", status)
    elif budget.spent() >= cfg.monthly_budget_usd:
        status = "budget nået"
        log.warning(
            "Månedens budget på %.2f USD er nået (brugt %.2f USD) - Haikus oversættelse gemmes uden korrektur",
            cfg.monthly_budget_usd,
            budget.spent(),
        )
    else:
        log.info("Korrektur med %s (effort %s)", cfg.proofread_model, cfg.proofread_effort)
        try:
            pr = proofread(body, en_index, before, cfg, client, budget, item_key, deadline=deadline, clock=clock)
            if pr.timed_out:
                status = f"delvis (tidsgrænsen på {cfg.postprocess_timeout_minutes:g} minutter nået)"
            else:
                status = "delvis (budget nået)" if pr.budget_stopped else "udført"
            if pr.batches_failed:
                status += f", {pr.batches_failed} bidder fejlede (replik {', '.join(pr.failed_ranges)})"
        except Exception:
            log.exception("Korrekturen fejlede uventet - filen gemmes uden korrektur, men med kreditlinje")
            status = "fejlede uventet"

    fixed = [replace(c, text=pr.fixes.get(pos, c.text)) for pos, c in enumerate(body)]
    after = check_all(fixed, en_index, cfg.max_line_chars, bad_words)
    if after:
        examples = "; ".join(f"#{p + 1} {','.join(f)}" for p, f in list(after.items())[:5])
        log.info("Stadig flaget efter korrektur: %d (%s) fx %s", len(after), _flag_summary(after), examples)
    else:
        log.info("Ingen flag tilbage efter korrektur")

    final, credit = apply_credit(fixed, cfg.credit_text, had_header)
    return PostResult(
        cues=final,
        flags_before=len(before),
        flags_after=len(after),
        fixes=len(pr.fixes),
        rejected=pr.rejected,
        cost=pr.cost,
        proofread=status,
        credit=credit,
        remaining=after,
        model=cfg.proofread_model,
    )


def make_client(cfg: Config):
    """Opret Anthropic-klienten, eller None hvis nøglen mangler."""
    key = cfg.anthropic_api_key()
    if not key:
        return None
    import anthropic

    # SDK'ens timeout gælder hver læsning; den hårde grænse for hele kaldet ligger i proofread (call_with_timeout).
    timeout = max(1.0, cfg.proofread_call_timeout_minutes * 60)
    return anthropic.Anthropic(api_key=key, timeout=anthropic.Timeout(timeout, connect=30.0), max_retries=2)
