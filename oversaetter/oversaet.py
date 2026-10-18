"""Testtilstand oversaet-test: oversæt en hel engelsk srt-fil i bidder med sammenhæng - uden Lingarr og korrektur.

Formål (besluttet 18.10.2026): sammenligne modeller i én gennemgang for at se, om Lingarrs linje-for-linje-
oversættelse og Opus-korrekturen kan droppes. Rører ikke driften: ingen database, intet budget, ingen kreditlinje,
og der skrives kun til --out.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace

from .checks import INTERJECTIONS, NOT_NAMES, WORD_RE, EnglishIndex, check_all, plain
from .config import Config, model_price, read_key
from .deadline import call_with_timeout
from .srt import Cue, read_file, write_file_atomic

log = logging.getLogger("oversaetter")

PROVIDERS = ("anthropic", "deepseek", "gemini")
BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}
PROVIDER_NAMES = {"anthropic": "Anthropic", "deepseek": "DeepSeek", "gemini": "Gemini"}
CONTEXT_BEFORE = 5
CONTEXT_AFTER = 3
GLOSSARY_SIZE = 30
MAX_TOKENS = 16000
TEMPERATURE = 0.3

# Lingarr-prompt v3, regel 1-15. Regel 8 og 15 er tilpasset til bidder.
SYSTEM_PROMPT = """You are an experienced Danish subtitler translating from English to Danish for Danish viewers. \
You receive a batch of numbered English subtitle cues as JSON in "lines". Translate each number on its own and \
keep its number "i".
1. Write natural spoken Danish, the way Danes actually talk. Translate the meaning, not word by word.
2. Use only Danish words and Danish spelling with æ, ø and å. Never use Norwegian, Swedish or German words or letters.
3. Translate every English word except names. These short words may stay: okay, hey, wow, yo, cool, nice, shit, \
fuck, bro.
4. Watch out for English spelling sneaking in: "her" (not "here"), "så" (not "so"), "lige" (not "llige"), \
"vel" (not "well").
5. Keep it short: at most 2 lines and at most 40 characters per line. Leave out filler words if needed, never \
information that matters.
6. Line breaks: if the source has two lines, use two lines. Split long sentences at a natural place.
7. Two speakers: keep "-" at the start of both Danish lines when the source has them. Never add "-" otherwise.
8. Half sentences: a cue is often part of a sentence that continues in the next cue. Translate only the words in \
that cue and keep them under its own number, but make the sentence read correctly across cues (e.g. "ikke tid / \
til at vente"). Never move words to another number, and never merge or split numbers.
9. Numbers keep their value. Digits stay digits. Numbers written as words become Danish words.
10. Names of people, places, companies, groups, shows and brands stay unchanged.
11. Keep negations correct. "Do not disrespect my science" means "Vis lidt respekt for min videnskab".
12. Swearing and insults: same strength as the source. Plural stays plural.
13. Slang: translate what the character means. "Sue me." -> "Så sagsøg mig." "That's it, JD." (angry) -> \
"Nu er det nok, JD." "Let's go." -> "Kom så." "Fancy." -> "Fint." "Executive decision." -> "Det bestemmer jeg." \
"frequent-flyer miles" -> "bonuspoint" "smelling bacon" (police) -> "lugter strisser" "My man!" -> "Sådan, mand!" \
"I like her ink" -> "Jeg kan lide hendes tatoveringer" "Bet." -> "Aftale." "ferocious rats" -> "glubske rotter" \
"beast" -> "uhyre" (never "bestie") "either way" -> "uanset hvad"
14. Be consistent: use the same Danish word for a name or recurring term every time. "glossary" shows how names \
and recurring terms were translated earlier in this file: follow it.
15. "context_before" (the previous cues with their finished Danish translation) and "context_after" (the next \
cues) are only for understanding. Never translate, repeat or return them. Reply with JSON only, no notes.

Format: keep formatting tags such as <i>...</i>. Keep the line breaks of the source and write a line break as \
\\n (at most 2 lines, at most 40 characters per line). Reply ONLY with JSON of the form \
{"lines":[{"i":<number>,"da":"<Danish text>"}]} with exactly one entry for every number in "lines" and no others."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "da": {"type": "string"}},
                "required": ["i", "da"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}


class KeyMissing(Exception):
    """Nøglen til en provider mangler. Teksten er dansk og vises direkte."""


@dataclass
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    stop: str  # "ok", "refusal", "max_tokens", "content_filter" ...


@dataclass
class TranslateResult:
    da: list[str]
    fallback: list[int] = field(default_factory=list)  # positioner (0-baseret) der beholdt den engelske tekst
    batches: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage: dict[str, list[int]] = field(default_factory=dict)  # model -> [tokens ind, tokens ud]
    by_fallback: int = 0  # replikker oversat af fallback-modellen efter refusal
    timed_out: bool = False  # tidsgrænsen (deadline) blev nået, før alle bidder var oversat


# --- klienter ------------------------------------------------------------------------------------------
def make_client(provider: str, cfg: Config):
    """Opret klienten til provideren. Rejser KeyMissing med en dansk besked, hvis nøglefilen mangler."""
    timeout = max(1.0, cfg.proofread_call_timeout_minutes * 60)
    if provider == "anthropic":
        path, env = cfg.anthropic_api_key_file, "ANTHROPIC_API_KEY_FILE"
    elif provider == "deepseek":
        path, env = cfg.deepseek_api_key_file, "DEEPSEEK_API_KEY_FILE"
    else:
        path, env = cfg.gemini_api_key_file, "GEMINI_API_KEY_FILE"
    key = read_key(path)
    if not key:
        why = "er en mappe, ikke en fil" if os.path.isdir(path) else "findes ikke, kan ikke læses eller er tom"
        raise KeyMissing(
            f"Nøglen til {PROVIDER_NAMES[provider]} mangler: {path} {why}. Læg nøglen i filen (se README under "
            f'"Testtilstand: oversæt en hel fil") eller ret {env} i .env. Driften er ikke påvirket.'
        )
    if provider == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=key, timeout=anthropic.Timeout(timeout, connect=30.0), max_retries=2)
    import openai

    return openai.OpenAI(
        api_key=key, base_url=BASE_URLS[provider], timeout=openai.Timeout(timeout, connect=30.0), max_retries=2
    )


def _int(obj, name: str) -> int:
    return int(getattr(obj, name, 0) or 0)


def ask_model(provider: str, client, model: str, effort: str | None, system: str, user: str) -> Reply:
    if provider == "anthropic":
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort or "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        usage = getattr(response, "usage", None)
        inp = sum(_int(usage, n) for n in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        text = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), "")
        stop = getattr(response, "stop_reason", None)
        return Reply(text, inp, _int(usage, "output_tokens"), "ok" if stop in ("end_turn", None) else str(stop))

    kwargs = {}
    if provider == "gemini" and effort:
        kwargs["reasoning_effort"] = effort
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=TEMPERATURE,
        **kwargs,
    )
    usage = getattr(response, "usage", None)
    inp, out = _int(usage, "prompt_tokens"), _int(usage, "completion_tokens")
    out = max(out, _int(usage, "total_tokens") - inp)  # tænke-tokens tælles ikke altid med i completion_tokens
    choice = response.choices[0]
    finish = getattr(choice, "finish_reason", None)
    text = getattr(choice.message, "content", None) or ""
    return Reply(text, inp, out, "ok" if finish in ("stop", None) else str(finish))


# --- svar og kontekst ----------------------------------------------------------------------------------
def parse_lines(text: str) -> list:
    """Find {"lines": [...]} i svaret (tåler ```json-hegn omkring). Rejser ValueError ved ugyldigt svar."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("intet JSON-objekt i svaret")
    data = json.loads(text[start : end + 1])
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list):
        raise ValueError('"lines" mangler eller er ikke en liste')
    return lines


def clean_da(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\\n", "\n")
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def valid_lines(lines: list, wanted: set[int]) -> tuple[dict[int, str], list[str]]:
    """Gyldige oversættelser {nummer: tekst} og en liste med problemer (manglende, ekstra eller tomme numre)."""
    found: dict[int, str] = {}
    extra: list = []
    empty: list[int] = []
    for item in lines:
        i = item.get("i") if isinstance(item, dict) else None
        da = item.get("da") if isinstance(item, dict) else None
        if not isinstance(i, int) or isinstance(i, bool) or i not in wanted:
            extra.append(i)
            continue
        if not isinstance(da, str) or not plain(clean_da(da)).strip():
            empty.append(i)
            continue
        found.setdefault(i, clean_da(da))
    problems = []
    missing = sorted(wanted - found.keys() - set(empty))
    if missing:
        problems.append("mangler " + _numbers(missing))
    if empty:
        problems.append("tomme " + _numbers(sorted(empty)))
    if extra:
        problems.append("ekstra " + ", ".join(str(x) for x in extra[:5]))
    return found, problems


def _numbers(nums: list[int]) -> str:
    shown = ", ".join(str(n) for n in nums[:8])
    return shown + (f" (+{len(nums) - 8})" if len(nums) > 8 else "")


SENTENCE_START = '.!?-:"\n…♪(['


def _words(text: str):
    """(ord, står først i en sætning) for hvert ord i teksten."""
    text = plain(text)
    for m in WORD_RE.finditer(text):
        before = text[: m.start()].rstrip(" \t")
        yield m.group(0), not before or before[-1] in SENTENCE_START


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[\w']+", plain(text).lower()))  # med tal, så "Rum 12" og "Rum 14" er forskellige


def find_names(en: list[str]) -> set[str]:
    """Ord med stort begyndelsesbogstav midt i en sætning, som aldrig står med småt i filen."""
    caps, lower = set(), set()
    for text in en:
        for w, first in _words(text):
            if w[0].islower():
                lower.add(w.lower())
            elif not first and len(w) > 1:
                caps.add(w)
    return {w for w in caps if w.lower() not in lower | NOT_NAMES | INTERJECTIONS}


class Glossary:
    """De seneste par (en -> da) med navne eller faste udtryk (korte replikker, der går igen i filen)."""

    def __init__(self, en: list[str]):
        names = find_names(en)
        counts = Counter(_norm(t) for t in en)
        self.useful = [
            any(w in names for w, _ in _words(t)) or (counts[_norm(t)] >= 2 and 0 < len(_norm(t).split()) <= 6)
            for t in en
        ]

    def pairs(self, en: list[str], da: list[str | None], before: int, skip_from: int, fallback: set[int]) -> list:
        out = []
        for pos in range(before - 1, -1, -1):
            if len(out) >= GLOSSARY_SIZE:
                break
            if pos >= skip_from or pos in fallback or da[pos] is None or not self.useful[pos]:
                continue
            out.append({"en": en[pos], "da": da[pos]})
        return out[::-1]


def build_user(en: list[str], da: list[str | None], start: int, end: int, glossary: list) -> str:
    before = range(max(0, start - CONTEXT_BEFORE), start)
    payload = {
        "glossary": glossary,
        "context_before": [{"i": p + 1, "en": en[p], "da": da[p]} for p in before if da[p] is not None],
        "lines": [{"i": p + 1, "en": en[p]} for p in range(start, end)],
        "context_after": [{"i": p + 1, "en": en[p]} for p in range(end, min(len(en), end + CONTEXT_AFTER))],
    }
    return (
        f'Translate the {end - start} cues in "lines" (numbers {start + 1}-{end}) into Danish and reply with JSON '
        '{"lines":[{"i":..,"da":".."}]}. "glossary", "context_before" and "context_after" are context only.\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )


# --- oversættelse --------------------------------------------------------------------------------------
def translate_all(
    en: list[str],
    provider: str,
    client,
    model: str,
    effort: str | None,
    batch: int,
    call_timeout: float,
    fallback_model: str | None = None,
    on_usage=None,
    deadline: float | None = None,
    clock=time.monotonic,
) -> TranslateResult:
    """Oversæt alle replikker i bidder.

    fallback_model: afvises en bid (refusal) også efter halvering, prøves den med denne model.
    on_usage(model, tokens_ind, tokens_ud): kaldes efter hvert svar (bruges til budgettet i driften).
    deadline: tidspunkt (efter clock) hvor oversættelsen stopper; resten beholder den engelske tekst.
    """
    n = len(en)
    da: list[str | None] = [None] * n
    result = TranslateResult(da=[])
    fallback: set[int] = set()
    glossary = Glossary(en)

    def attempt(start: int, end: int, use_model: str = model) -> tuple[dict[int, str], list[str], bool]:
        """Ét kald for replik start..end-1. Returnerer (gyldige oversættelser, problemer, afvist med refusal)."""
        wanted = set(range(start + 1, end + 1))
        timeout = call_timeout if deadline is None else min(call_timeout, deadline - clock())
        if timeout <= 0:
            result.timed_out = True
            return {}, ["tidsgrænsen for hele filen er nået"], False
        user = build_user(en, da, start, end, glossary.pairs(en, da, start, start - CONTEXT_BEFORE, fallback))
        result.calls += 1
        try:
            reply = call_with_timeout(ask_model, timeout, provider, client, use_model, effort, SYSTEM_PROMPT, user)
        except TimeoutError:
            return {}, [f"intet svar inden for {timeout:g} sekunder (timeout)"], False
        except Exception as e:  # netværk, API-fejl osv.
            return {}, [f"fejl fra {PROVIDER_NAMES[provider]}: {type(e).__name__}: {str(e)[:200]}"], False
        result.input_tokens += reply.input_tokens
        result.output_tokens += reply.output_tokens
        used = result.usage.setdefault(use_model, [0, 0])
        used[0] += reply.input_tokens
        used[1] += reply.output_tokens
        if on_usage is not None:
            on_usage(use_model, reply.input_tokens, reply.output_tokens)
        if reply.stop != "ok":
            return {}, [f"svaret blev afbrudt (stop={reply.stop})"], reply.stop == "refusal"
        try:
            lines = parse_lines(reply.text)
        except (ValueError, TypeError) as e:
            return {}, [f"ugyldig JSON ({e})"], False
        return (*valid_lines(lines, wanted), False)

    def keep(found: dict[int, str]) -> None:
        for i, text in found.items():
            da[i - 1] = text

    def run(start: int, end: int, label: str) -> None:
        found, problems, _ = attempt(start, end)
        if not problems:
            keep(found)
            return
        log.warning("Bid %s (replik %d-%d): %s - prøver igen", label, start + 1, end, "; ".join(problems))
        found, problems, refused = attempt(start, end)
        if not problems:
            keep(found)
            return
        if end - start < 2:
            last_try(start, end, found, problems, refused)
            return
        mid = (start + end + 1) // 2
        log.warning(
            "Bid %s (replik %d-%d): %s - prøver igen delt i to: %d-%d og %d-%d",
            label,
            start + 1,
            end,
            "; ".join(problems),
            start + 1,
            mid,
            mid + 1,
            end,
        )
        for a, b in ((start, mid), (mid, end)):
            found, problems, refused = attempt(a, b)
            if problems:
                last_try(a, b, found, problems, refused)
            else:
                keep(found)

    def last_try(start: int, end: int, found: dict[int, str], problems: list[str], refused: bool) -> None:
        """Afvist (refusal) også efter halvering: prøv med fallback-modellen, før replikkerne beholder engelsk."""
        if refused and fallback_model and fallback_model != model:
            log.warning(
                "Replik %d-%d blev afvist af %s også efter halvering - prøver med fallback-modellen %s",
                start + 1,
                end,
                model,
                fallback_model,
            )
            keep(found)
            missing = [p for p in range(start, end) if da[p] is None]
            if not missing:
                return
            a, b = missing[0], missing[-1] + 1
            found, problems, _ = attempt(a, b, fallback_model)
            if not problems:
                keep(found)
                result.by_fallback += b - a
                log.info("Replik %d-%d er oversat med fallback-modellen %s", a + 1, b, fallback_model)
                return
            problems = [f"også {fallback_model}: " + "; ".join(problems)]
            start, end = a, b
        give_up(start, end, found, problems)

    def give_up(start: int, end: int, found: dict[int, str], problems: list[str]) -> None:
        """Behold de gyldige oversættelser; resten beholder den engelske tekst."""
        keep(found)
        missing = [p for p in range(start, end) if da[p] is None]
        if missing:
            log.error(
                "Replik %s beholder den engelske tekst (%s)", _numbers([p + 1 for p in missing]), "; ".join(problems)
            )
            for p in missing:
                da[p] = en[p]
                fallback.add(p)

    size = max(1, batch)
    starts = list(range(0, n, size))
    result.batches = len(starts)
    for k, start in enumerate(starts, start=1):
        end = min(n, start + size)
        label = f"{k}/{len(starts)}"
        if deadline is not None and clock() >= deadline:
            result.timed_out = True
            log.warning("Tidsgrænsen er nået ved bid %s - replik %d-%d oversættes ikke", label, start + 1, n)
            break
        log.info("Oversætter bid %s (replik %d-%d) med %s", label, start + 1, end, model)
        try:
            run(start, end, label)
        except Exception:  # aldrig lade én bid stoppe resten
            log.exception("Uventet fejl i bid %s - replikkerne beholder den engelske tekst", label)
        for p in range(start, end):
            if da[p] is None:
                da[p] = en[p]
                fallback.add(p)
    for p in range(n):
        if da[p] is None:
            da[p] = en[p]
            fallback.add(p)
    result.da = [t if t is not None else en[p] for p, t in enumerate(da)]
    result.fallback = sorted(fallback)
    return result


# --- testtilstanden ------------------------------------------------------------------------------------
def _duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s} s" if m else f"{s} s"


def run_translate_test(
    cfg: Config,
    en_path: str,
    out: str,
    provider: str,
    model: str,
    effort: str | None = None,
    batch: int = 50,
    client=None,
    clock=time.monotonic,
) -> int:
    if os.path.abspath(out) == os.path.abspath(en_path):
        log.error("--out må ikke være den samme fil som --en")
        return 2
    if os.path.exists(out):
        log.error("%s findes allerede - vælg et nyt filnavn", out)
        return 2
    if provider not in PROVIDERS:
        log.error("Ukendt provider %r - brug %s", provider, ", ".join(PROVIDERS))
        return 2
    try:
        en_cues = read_file(en_path)
    except OSError as e:
        log.error("Kan ikke læse den engelske fil %s (%s)", en_path, e.strerror or type(e).__name__)
        return 2
    if not en_cues:
        log.error("Der er ingen replikker i %s", en_path)
        return 2
    if client is None:
        try:
            client = make_client(provider, cfg)
        except KeyMissing as e:
            log.error("%s", e)
            return 2
    if effort and provider == "deepseek":
        log.warning("--effort bruges ikke hos DeepSeek og ignoreres")
        effort = None

    started = clock()
    log.info("oversaet-test: %d replikker, %s/%s, bidder af %d", len(en_cues), provider, model, batch)
    en = [c.text for c in en_cues]
    call_timeout = max(1.0, cfg.proofread_call_timeout_minutes * 60)
    res = translate_all(en, provider, client, model, effort, batch, call_timeout)
    da_cues: list[Cue] = [replace(c, text=t) for c, t in zip(en_cues, res.da, strict=True)]
    write_file_atomic(out, da_cues)  # ingen kreditlinje i testfilen
    elapsed = clock() - started

    flags = check_all(da_cues, EnglishIndex(en_cues), cfg.max_line_chars, cfg.bad_words())
    counts = Counter(f for fl in flags.values() for f in fl)
    cost = model_price(model, res.input_tokens, res.output_tokens)
    examples = [f"  #{p + 1} {', '.join(fl)}: {da_cues[p].text.replace(chr(10), ' / ')}" for p, fl in flags.items()]
    report = "\n".join(
        [
            "Testrapport (oversaet-test)",
            f"Engelsk kilde: {en_path}",
            f"Resultat: {out}",
            f"Provider: {provider}",
            f"Model: {model} (effort {effort or '-'})",
            f"Replikker: {len(en_cues)}",
            f"Bidder: {res.batches} (kald i alt: {res.calls})",
            f"Tokens ind: {res.input_tokens}",
            f"Tokens ud: {res.output_tokens}",
            "Pris: " + ("pris ukendt" if cost is None else f"{cost:.4f} USD"),
            f"Tid: {_duration(elapsed)}",
            f"Flag fra kontrollen: {len(flags)}"
            + (" (" + ", ".join(f"{k}={v}" for k, v in counts.most_common()) + ")" if flags else ""),
            *(["Eksempler:", *examples[:10]] if examples else []),
            f"Faldt tilbage til engelsk: {len(res.fallback)}"
            + (f" (replik {_numbers([p + 1 for p in res.fallback])})" if res.fallback else ""),
            "Kreditlinje: ingen (testtilstand)",
        ]
    )
    print(report)
    return 0
