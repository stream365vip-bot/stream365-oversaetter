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
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from . import ordliste
from .checks import (
    INFO_FLAGS,
    INTERJECTIONS,
    NOT_NAMES,
    WORD_RE,
    EnglishIndex,
    check_all,
    count_info,
    fix_flags,
    only_filler,
    plain,
)
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

# Prompt v4 (18.10.2026): v3 med prioritering, færre regler og ét forkert/rigtigt eksempel pr. regel.
SYSTEM_PROMPT_V4 = """You are an experienced Danish subtitler translating from English to Danish for Danish viewers. \
You receive a batch of numbered English subtitle cues as JSON in "lines". Translate each number on its own and \
keep its number "i".
When rules conflict, this order wins: 1) the meaning is right, 2) at most 2 lines and at most 40 characters per \
line, 3) natural spoken Danish, 4) style and tone.
1. Meaning, not words: write Danish the way Danes actually talk. Wrong: "Det er ikke min kop te." Right: "Det er \
ikke lige mig."
2. Only Danish words and spelling with æ, ø and å, never Norwegian, Swedish or German. Translate every English word \
except names; only these may stay: okay, hey, wow, yo, cool, nice, shit, fuck, bro. Wrong: "Uansett hva, please." \
Right: "Uanset hvad, kom nu."
3. Anglicisms: translate what is meant. "I mean" -> "altså", "like" (filler) -> leave out or "ligesom", "you know" \
-> "du ved" or leave out, "whatever" -> "og hvad så"/"lige meget", "deal with it" -> "lev med det", "I'm good" -> \
"ellers tak"/"jeg klarer mig", "no way" -> "glem det"/"det kan ikke passe", "at the end of the day" -> "når alt \
kommer til alt", "reach out" -> "kontakte", "off the record" -> "mellem os", "check" (confirming) -> "tjek", \
"yikes" -> "uha", "please" (begging) -> "kom nu"/"vær sød". Wrong: "Jeg mener, det er fint." Right: "Altså, det er \
fint."
4. Generic "you" (anyone) becomes "man". "You" said to a person stays "du"/"I". Wrong: "You never know." -> "Du ved \
aldrig." Right: "Man ved aldrig."
5. Compound nouns are one word. Wrong: "bil nøgle", "nattesyns briller". Right: "bilnøgle", "hovednøgle", \
"nattesynsbriller".
6. Lower case for weekdays, months, nationalities and languages. Wrong: "på Mandag i Juni", "Han er Svensk." Right: \
"på mandag i juni", "Han er svensk."
7. Filler sounds (uh, um, hm, hmm, ah, ugh) are left out. A cue with only filler sounds gets an empty "da": "". \
"Yeah." / "Okay." / "Right." / "Sure." alone -> "Ja." / "Okay." / "Nemlig." / "Klart." Wrong: "Øh, jeg ved det \
ikke." Right: "Jeg ved det ikke."
8. Short: at most 2 lines and at most 40 characters per line. Leave out filler, never information that matters. If \
the source has two lines, use two lines, and split long sentences at a natural place. Wrong: "Jeg har overhovedet \
ikke tænkt mig at tage med dig derhen i aften." Right: "Jeg tager ikke med\\ndig derhen i aften."
9. Half sentences: a cue is often part of a sentence that continues in the next cue. Translate only the words in \
that cue under its own number, but make the sentence read correctly across cues. Never move words to another \
number, and never merge or split numbers. Wrong: 12 "Jeg har ikke tid til at vente", 13 "". Right: 12 "Jeg har \
ikke tid", 13 "til at vente."
10. Keep <i>...</i> and speaker dashes: "-" at the start of both lines when the source has them, never added \
otherwise. Wrong: "<i>Hvor er du?" Right: "<i>Hvor er du?</i>"
11. Numbers keep their value. Digits stay digits, numbers written as words become Danish words. Wrong: "three \
days" -> "3 dage". Right: "tre dage".
12. Names of people, places, companies, groups, shows and brands stay unchanged. Song lyrics and signs are \
translated unless they are names. Wrong: "John" -> "Jens". Right: "John".
13. Other foreign languages in the source (French, Spanish ...) stay exactly as written. Wrong: "Merci." -> "Tak." \
Right: "Merci."
14. Negations stay correct. Wrong: "Do not disrespect my science" -> "Respektér ikke min videnskab." Right: "Vis \
lidt respekt for min videnskab." A yes-answer to a negative question keeps its meaning: "Diane har vel ikke noget \
imod det?" - "Definitely." Wrong: "Bestemt ikke." Right: "Nej, slet ikke." or "Helt sikkert."
15. Swearing, insults and slang: same strength as the source, plural stays plural, and translate what the \
character means. "Sue me." -> "Så sagsøg mig." "Let's go." -> "Kom så." "Executive decision." -> "Det bestemmer \
jeg." "Bet." -> "Aftale." "beast" -> "uhyre" (never "bestie"). Wrong: "Fancy." -> "Fancy." Right: "Fint."
Fixed translations (never leave these in English): "bitch" -> "kælling", "bitches" -> "kællinger", "dude" -> \
"mand"/"makker", "nobody"/"nobodies" (a loser) -> "et nul"/"nuller", "crew" -> "bande"/"hold", "bossman" -> \
"chefen", "Mr."/"Mrs." -> "hr."/"fru", "guys" -> "folkens"/"venner", "keep shooting" (with a gun) -> "bliv ved med \
at skyde". Translate "Oh my God"/"Jesus" consistently as "Åh gud" (not "Herregud" in between), unless the film \
glossary says otherwise.
16. Consistency: use the same Danish word for a name or recurring term every time. Follow the film glossary (if \
given) exactly and use its "address" (du/De) between the same characters through the whole film. "glossary" in the \
request shows how names and recurring terms were translated earlier in this file: follow it. Wrong: "Store Mike" \
in one cue and "Big Mike" in the next. Right: the same every time.
17. "context_before" (the previous cues with their finished Danish translation) and "context_after" (the next \
cues) are only for understanding. Never translate, repeat or return them. Reply with JSON only, no notes.

Format: write a line break as \\n. Reply ONLY with JSON of the form {"lines":[{"i":<number>,"da":"<Danish text>"}]} \
with exactly one entry for every number in "lines" and no others."""

# Kun for deepseek/gemini i v4: de tænker bedst, når de bliver bedt om det.
THINK_FIRST = (
    "First make a rough translation in your own thoughts, review it for anglicisms and for lines that are too long, "
    "and output only the finished JSON."
)


def build_system(version: str, provider: str, context: str | None = None, glossary=None) -> str:
    """Systeminstruksen: v3 eller v4, evt. med filmkontekst og ordliste (samme for alle bidder, så den caches)."""
    parts = [SYSTEM_PROMPT_V4 if version == "v4" else SYSTEM_PROMPT]
    if version == "v4" and provider in ("deepseek", "gemini"):
        parts[0] += "\n" + THINK_FIRST
    if context:
        parts.append(context)
    block = glossary.block() if glossary is not None else ""
    if block:
        parts.append(block)
    return "\n\n".join(parts)


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
    input_tokens: int  # almindelige tokens ind (uden prompt-cachens)
    output_tokens: int
    stop: str  # "ok", "refusal", "max_tokens", "content_filter" ...
    cache_read: int = 0  # tokens læst fra prompt-cachen (10 % af inputprisen)
    cache_write: int = 0  # tokens skrevet til prompt-cachen (125 % af inputprisen)


@dataclass
class TranslateResult:
    da: list[str]
    fallback: list[int] = field(default_factory=list)  # positioner (0-baseret) der beholdt den engelske tekst
    batches: int = 0
    calls: int = 0
    input_tokens: int = 0  # uden prompt-cachens tokens
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    usage: dict[str, list[int]] = field(default_factory=dict)  # model -> [ind, ud, cache-læs, cache-skriv]
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


def ask_model(
    provider: str,
    client,
    model: str,
    effort: str | None,
    system: str,
    user: str,
    schema: dict | None = None,
    cache: bool = False,
) -> Reply:
    """Ét kald. cache=True: systeminstruksen markeres til Anthropics prompt-cache (samme for alle bidder)."""
    if provider == "anthropic":
        sys_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cache else system
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=sys_param,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": effort or "medium",
                "format": {"type": "json_schema", "schema": schema or OUTPUT_SCHEMA},
            },
        )
        usage = getattr(response, "usage", None)
        text = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), "")
        stop = getattr(response, "stop_reason", None)
        return Reply(
            text,
            _int(usage, "input_tokens"),
            _int(usage, "output_tokens"),
            "ok" if stop in ("end_turn", None) else str(stop),
            _int(usage, "cache_read_input_tokens"),
            _int(usage, "cache_creation_input_tokens"),
        )

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


def valid_lines(lines: list, wanted: set[int], empty_ok: set[int] = frozenset()) -> tuple[dict[int, str], list[str]]:
    """Gyldige oversættelser {nummer: tekst} og en liste med problemer (manglende, ekstra eller tomme numre).

    empty_ok: numre, der må få tom tekst eller mangle i svaret (v4: replikker, der kun består af fyldlyde).
    """
    found: dict[int, str] = {}
    extra: list = []
    empty: list[int] = []
    for item in lines:
        i = item.get("i") if isinstance(item, dict) else None
        da = item.get("da") if isinstance(item, dict) else None
        if not isinstance(i, int) or isinstance(i, bool) or i not in wanted:
            extra.append(i)
            continue
        if isinstance(da, str) and i in empty_ok and not plain(clean_da(da)).strip():
            found.setdefault(i, "")
            continue
        if not isinstance(da, str) or not plain(clean_da(da)).strip():
            empty.append(i)
            continue
        found.setdefault(i, clean_da(da))
    for i in empty_ok & wanted:
        if i not in found and i not in empty:
            found[i] = ""  # en udeladt fyldlyd tæller som tom - intet genforsøg
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


# --- ordliste og tomme replikker ------------------------------------------------------------------------
def make_glossary(
    provider: str,
    client,
    model: str,
    effort: str | None,
    en: list[str],
    context: str | None,
    previous: ordliste.Wordlist | None,
    timeout: float,
    on_usage=None,
) -> ordliste.Wordlist | None:
    """Ordlisten pr. film med ét kald til samme model som oversættelsen. None (med en WARNING) ved fejl."""

    def ask(system: str, user: str, schema: dict) -> Reply:
        return call_with_timeout(ask_model, timeout, provider, client, model, effort, system, user, schema)

    def usage(inp: int, out: int) -> None:
        if on_usage is not None:
            on_usage(model, inp, out, 0, 0)

    wl = ordliste.build(ask, en, context, previous, usage)
    if wl is not None:
        wl.cost = model_price(model, wl.input_tokens, wl.output_tokens)
    return wl


def filler_positions(en: list[str], version: str) -> set[int]:
    """v4: positioner, hvor replikken kun er fyldlyde/interjektioner (checks.FILLER) og må få tom tekst."""
    return {p for p, t in enumerate(en) if only_filler(t)} if version == "v4" else set()


def drop_empty(da: list[str], fallback: list[int]) -> tuple[list[int], list[int]]:
    """Positioner der beholdes (tomme replikker fjernes) og fallback-positionerne omregnet til de nye positioner."""
    kept = [p for p, t in enumerate(da) if plain(t).strip()]
    new = {p: i for i, p in enumerate(kept)}
    return kept, sorted(new[p] for p in fallback if p in new)


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
    system: str | None = None,
    parallel: int = 1,
    empty_ok: set[int] = frozenset(),
    cache: bool | None = None,
) -> TranslateResult:
    """Oversæt alle replikker i bidder.

    fallback_model: afvises en bid (refusal) også efter halvering, prøves den med denne model.
    on_usage(model, tokens_ind, tokens_ud, cache_læs, cache_skriv): kaldes efter hvert svar (budgettet i driften).
    deadline: tidspunkt (efter clock) hvor oversættelsen stopper; resten beholder den engelske tekst.
    system: systeminstruksen (standard v3 uden kontekst).
    cache: marker systeminstruksen til Anthropics prompt-cache (PROMPT_CACHE). None: kun en anden instruks end v3.
    parallel: så mange bidder sendes samtidig (tråde). De danske "foregående" linjer sendes kun, når de er færdige.
    empty_ok: positioner (0-baseret), der må få tom tekst (v4: kun fyldlyde).
    """
    n = len(en)
    da: list[str | None] = [None] * n
    result = TranslateResult(da=[])
    fallback: set[int] = set()
    glossary = Glossary(en)
    system = system or SYSTEM_PROMPT
    if cache is None:
        cache = system != SYSTEM_PROMPT
    lock = threading.RLock()  # bogføring (tokens, budget, da, fallback) fra flere tråde

    def attempt(start: int, end: int, use_model: str = model) -> tuple[dict[int, str], list[str], bool]:
        """Ét kald for replik start..end-1. Returnerer (gyldige oversættelser, problemer, afvist med refusal)."""
        wanted = set(range(start + 1, end + 1))
        timeout = call_timeout if deadline is None else min(call_timeout, deadline - clock())
        if timeout <= 0:
            result.timed_out = True
            return {}, ["tidsgrænsen for hele filen er nået"], False
        with lock:
            user = build_user(en, da, start, end, glossary.pairs(en, da, start, start - CONTEXT_BEFORE, fallback))
            result.calls += 1
        try:
            reply = call_with_timeout(
                ask_model, timeout, provider, client, use_model, effort, system, user, None, cache
            )
        except TimeoutError:
            return {}, [f"intet svar inden for {timeout:g} sekunder (timeout)"], False
        except Exception as e:  # netværk, API-fejl osv.
            return {}, [f"fejl fra {PROVIDER_NAMES[provider]}: {type(e).__name__}: {str(e)[:200]}"], False
        with lock:
            result.input_tokens += reply.input_tokens
            result.output_tokens += reply.output_tokens
            result.cache_read += reply.cache_read
            result.cache_write += reply.cache_write
            used = result.usage.setdefault(use_model, [0, 0, 0, 0])
            used[0] += reply.input_tokens
            used[1] += reply.output_tokens
            used[2] += reply.cache_read
            used[3] += reply.cache_write
            if on_usage is not None:
                on_usage(use_model, reply.input_tokens, reply.output_tokens, reply.cache_read, reply.cache_write)
        if reply.stop != "ok":
            return {}, [f"svaret blev afbrudt (stop={reply.stop})"], reply.stop == "refusal"
        try:
            lines = parse_lines(reply.text)
        except (ValueError, TypeError) as e:
            return {}, [f"ugyldig JSON ({e})"], False
        return (*valid_lines(lines, wanted, {p + 1 for p in empty_ok if start <= p < end}), False)

    def keep(found: dict[int, str]) -> None:
        with lock:
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
        with lock:
            missing = [p for p in range(start, end) if da[p] is None]
            if missing:
                log.error(
                    "Replik %s beholder den engelske tekst (%s)",
                    _numbers([p + 1 for p in missing]),
                    "; ".join(problems),
                )
                for p in missing:
                    da[p] = en[p]
                    fallback.add(p)

    size = max(1, batch)
    starts = list(range(0, n, size))
    result.batches = len(starts)
    stop = threading.Event()  # tidsgrænsen er nået: start ingen nye bidder

    def one_batch(k: int, start: int) -> None:
        end = min(n, start + size)
        label = f"{k}/{len(starts)}"
        if stop.is_set():
            return
        if deadline is not None and clock() >= deadline:
            result.timed_out = True
            stop.set()
            log.warning("Tidsgrænsen er nået ved bid %s - replik %d og frem oversættes ikke", label, start + 1)
            return
        log.info("Oversætter bid %s (replik %d-%d) med %s", label, start + 1, end, model)
        try:
            run(start, end, label)
        except Exception:  # aldrig lade én bid stoppe resten
            log.exception("Uventet fejl i bid %s - replikkerne beholder den engelske tekst", label)
        with lock:
            for p in range(start, end):
                if da[p] is None:
                    da[p] = en[p]
                    fallback.add(p)

    if parallel <= 1 or len(starts) <= 1:
        for k, start in enumerate(starts, start=1):
            one_batch(k, start)
            if stop.is_set():
                break
    else:
        # Bidderne sendes i rækkefølge; højst `parallel` ad gangen. Fejl i én bid stopper ikke de andre.
        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="bid") as pool:
            for future in [pool.submit(one_batch, k, start) for k, start in enumerate(starts, start=1)]:
                future.result()
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
    prompt: str = "v3",
    context: bool = False,
    glossary: bool = False,
    tmdb_id: str | None = None,
    parallel: int = 1,
    http=None,
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
    if prompt not in ("v3", "v4"):
        log.error("Ukendt --prompt %r - brug v3 eller v4", prompt)
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
    parallel = max(1, parallel)
    on = {True: "on", False: "off"}
    switches = f"prompt {prompt}, kontekst {on[context]}, ordliste {on[glossary]}, parallel {parallel}"
    cache = cfg.prompt_cache and provider == "anthropic"
    switches += f", prompt-cache {on[cache]}"

    started = clock()
    log.info("oversaet-test: %d replikker, %s/%s, bidder af %d, %s", len(en_cues), provider, model, batch, switches)
    en = [c.text for c in en_cues]
    call_timeout = max(1.0, cfg.proofread_call_timeout_minutes * 60)

    ctx_text = None
    if context:
        ctx_text = _test_context(cfg, tmdb_id, http)
    wordlist = None
    if glossary:
        wordlist = make_glossary(provider, client, model, effort, en, ctx_text, None, call_timeout)
    system = build_system(prompt, provider, ctx_text, wordlist)
    res = translate_all(
        en,
        provider,
        client,
        model,
        effort,
        batch,
        call_timeout,
        system=system,
        parallel=parallel,
        empty_ok=filler_positions(en, prompt),
        cache=cache,
    )
    kept, fallback = drop_empty(res.da, res.fallback)
    da_cues: list[Cue] = [replace(en_cues[p], text=res.da[p]) for p in kept]
    write_file_atomic(out, da_cues)  # ingen kreditlinje i testfilen
    elapsed = clock() - started

    # De skarpere flag vises altid i rapporten, så v3 og v4 kan sammenlignes på samme kontrol.
    flags = check_all(
        da_cues,
        EnglishIndex(en_cues),
        cfg.max_line_chars,
        cfg.bad_words(),
        wordlist,
        strict=True,
        reading_speed_max=cfg.reading_speed_max,
    )
    # Informationsflag (fx reading_speed) tælles for sig: de ville ikke blive sendt i rettelsesrunden.
    fix = fix_flags(flags)
    counts = Counter(f for fl in fix.values() for f in fl)
    info_counts = Counter(f for fl in flags.values() for f in fl if f in INFO_FLAGS)
    inp, outp = res.input_tokens, res.output_tokens
    if wordlist is not None:
        inp, outp = inp + wordlist.input_tokens, outp + wordlist.output_tokens
    cost = model_price(model, inp, outp, res.cache_read, res.cache_write)
    examples = [f"  #{p + 1} {', '.join(fl)}: {da_cues[p].text.replace(chr(10), ' / ')}" for p, fl in fix.items()]
    if not glossary:
        glossary_line = "Ordliste: slået fra"
    elif wordlist is None:
        glossary_line = "Ordliste: kunne ikke laves (se loggen)"
    else:
        price = "pris ukendt" if wordlist.cost is None else f"{wordlist.cost:.4f} USD"
        glossary_line = f"Ordliste: {len(wordlist.terms)} termer, {len(wordlist.address)} tiltaler, {price}"
    report = "\n".join(
        [
            "Testrapport (oversaet-test)",
            f"Engelsk kilde: {en_path}",
            f"Resultat: {out}",
            f"Provider: {provider}",
            f"Model: {model} (effort {effort or '-'})",
            f"Kontakter: {switches}",
            "Filmkontekst: " + (f"{len(ctx_text)} tegn" if ctx_text else "ingen"),
            glossary_line,
            f"Replikker: {len(en_cues)}"
            + (f" (heraf {len(en_cues) - len(kept)} med kun fyldlyde fjernet)" if len(kept) < len(en_cues) else ""),
            f"Bidder: {res.batches} (kald i alt: {res.calls})",
            f"Tokens ind: {inp + res.cache_read + res.cache_write}"
            + (f" (heraf cache-læs {res.cache_read}, cache-skriv {res.cache_write})" if cache else ""),
            f"Tokens ud: {outp}",
            "Pris: " + ("pris ukendt" if cost is None else f"{cost:.4f} USD"),
            f"Tid: {_duration(elapsed)}",
            f"Rettelsesflag fra kontrollen: {len(fix)}"
            + (" (" + ", ".join(f"{k}={v}" for k, v in counts.most_common()) + ")" if fix else ""),
            f"Informationsflag (sendes ikke til rettelse): {count_info(flags)}"
            + (" (" + ", ".join(f"{k}={v}" for k, v in info_counts.most_common()) + ")" if info_counts else ""),
            *(["Eksempler:", *examples[:10]] if examples else []),
            f"Faldt tilbage til engelsk: {len(fallback)}"
            + (f" (replik {_numbers([p + 1 for p in fallback])})" if fallback else ""),
            "Kreditlinje: ingen (testtilstand)",
        ]
    )
    print(report)
    return 0


def _test_context(cfg: Config, tmdb_id: str | None, http) -> str | None:
    """Kontekst i testtilstanden: kun fra TMDB (--tmdb-id), uden Radarr-opslag."""
    from . import kontekst

    if not tmdb_id:
        log.warning("--context on uden --tmdb-id: oversætter uden kontekst")
        return None
    key = cfg.tmdb_key()
    if not key:
        log.warning("Ingen TMDB-nøgle i %s eller TMDB_API_KEY - oversætter uden kontekst", cfg.tmdb_api_key_file)
        return None
    if http is None:
        import httpx2

        http = httpx2.Client(timeout=httpx2.Timeout(60.0))
    return kontekst.for_tmdb_id(kontekst.Tmdb(cfg.tmdb_url, key, http), tmdb_id)
