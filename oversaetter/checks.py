"""Kontrol A: gratis tjek af hver dansk replik mod den engelske kilde."""

from __future__ import annotations

import bisect
import functools
import re

from .srt import Cue

TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")

# Ord der tyder på, at oversætteren har skrevet sine egne noter/overvejelser.
NOTE_RE = re.compile(
    r"\b(wait|hm+|let'?s see|translation|translated|translate|output|final|the source|line \d+|chars?)\b"
    r"|\bline\s*#?\d+",
    re.IGNORECASE,
)

# Almindelige engelske ord, som ikke også er almindelige danske ord.
ENGLISH_WORDS = {
    "the",
    "you",
    "your",
    "you're",
    "what",
    "this",
    "that",
    "that's",
    "with",
    "are",
    "was",
    "were",
    "it's",
    "don't",
    "i'm",
    "we're",
    "they're",
    "would",
    "could",
    "should",
    "there",
    "their",
    "know",
    "think",
    "just",
    "about",
    "because",
    "really",
    "going",
    "want",
    "from",
    "they",
    "have",
    "has",
    "been",
    "where",
    "when",
    "why",
    "how",
    "who",
    "can't",
    "won't",
    "didn't",
    "doesn't",
    "isn't",
    "aren't",
    "something",
    "nothing",
    "everything",
    "please",
    "thank",
    "thanks",
    "yes",
    "yeah",
    "come",
    "right",
    "here's",
    "let's",
    "gonna",
    "wanna",
    "into",
    "out",
    "our",
    "these",
    "those",
    "which",
    "said",
    "tell",
    "never",
    "always",
    "only",
    "need",
    "make",
    "take",
    "look",
    "back",
    "good",
    "well",
    "now",
    "then",
    "them",
    "him",
    "she",
    "he's",
    "she's",
    "his",
    "we",
    "me",
    "my",
    "is",
    "and",
    "of",
    "it",
    "do",
    "be",
}
# Ord der også er danske, så de ikke alene må udløse "engelsk sætning".
DANISH_HOMOGRAPHS = {"have", "is", "and", "be", "out", "her", "hans", "man", "for", "to", "i", "me", "well"}

NORDIC_WORDS = {
    # norsk
    "uansett",
    "leter",
    "lissom",
    "ikkje",
    "kanskje",
    "veldig",
    "takk",
    "hei",
    "jente",
    "gutt",
    "kjære",
    "skjønner",
    "sånn",
    "dritt",
    "faen",
    "jævla",
    "noen",
    "fortsatt",
    "kjenner",
    # svensk
    "bara",
    "inte",
    "och",
    "mycket",
    "tjej",
    "kille",
    "tack",
    "varför",
    "kanske",
    "något",
    "någon",
    "hur",
    "säger",
    "själv",
    "också",
    "bra",
}
NORDIC_LETTERS_RE = re.compile(r"[öäüÖÄÜ]")

MISSPELLINGS = [
    (re.compile(r"\bhere\b", re.IGNORECASE), "here"),
    (re.compile(r"\bso snart\b", re.IGNORECASE), "so snart"),
    (re.compile(r"\bllige\b", re.IGNORECASE), "llige"),
    (re.compile(r"\bwill\b", re.IGNORECASE), "will"),
]

# Ord der før er set forkert i Haikus oversættelser. Kan udvides med KNOWN_BAD_WORDS i .env.
KNOWN_BAD_WORDS = (
    "bestie",
    "bestien",
    "menneskebestie",
    "menneskebestien",
    "uansett",
    "please",
    "sweetheart",
    "prank",
    "llige",
    "here",
)
# Almindelige låneord, som aldrig må flages som kendte fejl - heller ikke hvis de står i KNOWN_BAD_WORDS.
ALLOWED_LOANWORDS = frozenset({"okay", "hey", "wow", "yo", "cool", "nice", "shit", "fuck", "bro"})


def known_bad_words(extra: str = "") -> frozenset[str]:
    """Startlisten plus kommaseparerede ord fra KNOWN_BAD_WORDS, uden de tilladte låneord."""
    words = set(KNOWN_BAD_WORDS) | {w.strip().lower() for w in extra.split(",") if w.strip()}
    return frozenset(words - ALLOWED_LOANWORDS)


@functools.lru_cache(maxsize=8)
def _bad_word_re(words: frozenset[str]) -> re.Pattern | None:
    if not words:
        return None
    alternatives = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


INTERJECTIONS = {
    "oh",
    "ah",
    "aah",
    "ahh",
    "uh",
    "um",
    "hmm",
    "hm",
    "hey",
    "okay",
    "ok",
    "wow",
    "yo",
    "cool",
    "nice",
    "shit",
    "fuck",
    "bro",
    "ha",
    "haha",
    "hah",
    "ooh",
    "ow",
    "ouch",
    "whoa",
    "huh",
    "mm",
    "mmm",
    "eh",
    "argh",
    "ugh",
    "phew",
    "psst",
    "shh",
    "bye",
    "hi",
    "no",
    "nej",
    "ja",
    "øh",
}

# Engelske ord med stort begyndelsesbogstav, som ikke er navne.
NOT_NAMES = ENGLISH_WORDS | {
    "hello",
    "sorry",
    "goodbye",
    "morning",
    "night",
    "help",
    "stop",
    "run",
    "go",
    "wait",
    "listen",
    "fine",
    "great",
    "sure",
    "maybe",
    "everyone",
    "guys",
    "dad",
    "mom",
    "mum",
    "mother",
    "father",
    "honey",
    "baby",
    "sir",
    "ma'am",
    "god",
    "what",
    "where",
    "why",
    "really",
    "seriously",
    "exactly",
    "careful",
    "quiet",
}

WORD_RE = re.compile(r"[A-Za-zÆØÅæøåÄÖÜäöüé']+")
NUMBER_RE = re.compile(r"\d+(?:[.,:]\d+)*")

BAZARR_HEADER_RE = re.compile(r"^\s*#\s*subtitles translated with\b.*#\s*$", re.IGNORECASE | re.DOTALL)

FLAG_TEXT = {
    "english_note": "contains an English note/reasoning from the translator",
    "english_sentence": "contains an untranslated English sentence",
    "too_many_lines": "more than 2 lines",
    "line_too_long": "a line is too long",
    "foreign_letters": "contains ö/ä/ü",
    "nordic_word": "contains a Norwegian/Swedish word",
    "english_misspelling": "contains a known English slip (here/will/so snart/llige)",
    "missing_number": "a number from the English line is missing",
    "empty": "empty line",
    "identical_to_english": "identical to the English line (not translated)",
    "known_bad_word": "contains a known wrong word (e.g. bestie, please, prank, sweetheart, uansett)",
}


def plain(text: str) -> str:
    return TAG_RE.sub("", text)


def is_bazarr_header(cue: Cue) -> bool:
    return bool(BAZARR_HEADER_RE.match(plain(cue.text).strip()))


def _words(text: str) -> list[str]:
    return [w.lower() for w in WORD_RE.findall(plain(text))]


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[.,:]", "", n) for n in NUMBER_RE.findall(plain(text))}


def _is_names_or_interjections(text: str) -> bool:
    raw = WORD_RE.findall(plain(text))
    if not raw:
        return True
    for w in raw:
        lw = w.lower()
        if lw in INTERJECTIONS:
            continue
        if w[0].isupper() and lw not in NOT_NAMES:
            continue  # navn
        return False
    return True


def _norm(text: str) -> str:
    return " ".join(_words(text))


def english_sentence(text: str) -> bool:
    words = _words(text)
    if not words:
        return False
    eng = [w for w in words if w in ENGLISH_WORDS]
    strong = {w for w in eng if w not in DANISH_HOMOGRAPHS}
    return len(strong) >= 3 or (len(strong) >= 2 and len(eng) >= 0.5 * len(words))


def has_english_note(text: str, en: str = "") -> bool:
    """Engelske note-ord tæller kun, hvis de ikke også står i den engelske replik."""
    en_lower = plain(en).lower()
    for m in NOTE_RE.finditer(plain(text)):
        if m.group(0).lower() not in en_lower:
            return True
    return False


def has_known_bad_word(text: str, words: frozenset[str] | None = None) -> bool:
    rx = _bad_word_re(known_bad_words() if words is None else words)
    return bool(rx and rx.search(plain(text)))


def check_cue(da: str, en: str, max_line_chars: int, bad_words: frozenset[str] | None = None) -> list[str]:
    flags: list[str] = []
    p = plain(da).strip()
    if not p:
        return ["empty"]
    if has_english_note(da, en):
        flags.append("english_note")
    if english_sentence(da):
        flags.append("english_sentence")
    lines = [line for line in plain(da).split("\n")]
    if len(lines) > 2:
        flags.append("too_many_lines")
    if any(len(line.strip()) > max_line_chars for line in lines):
        flags.append("line_too_long")
    if NORDIC_LETTERS_RE.search(p):
        flags.append("foreign_letters")
    lw = " ".join(_words(da))
    if any(re.search(rf"(?<![\wæøå]){re.escape(w)}(?![\wæøå])", lw) for w in NORDIC_WORDS):
        flags.append("nordic_word")
    if any(rx.search(p) for rx, _ in MISSPELLINGS):
        flags.append("english_misspelling")
    if has_known_bad_word(da, bad_words):
        flags.append("known_bad_word")
    if en:
        da_nums = _numbers(da)
        da_digits = re.sub(r"\D", "", plain(da))
        if any(n not in da_nums and n not in da_digits for n in _numbers(en)):
            flags.append("missing_number")
        if _norm(da) == _norm(en) and _norm(da) and not _is_names_or_interjections(en):
            flags.append("identical_to_english")
    return flags


class EnglishIndex:
    """Find den engelske replik der passer til en dansk replik ud fra tidskoderne."""

    def __init__(self, cues: list[Cue]):
        self.cues = sorted(cues, key=lambda c: c.start)
        self.starts = [c.start for c in self.cues]

    def text_for(self, cue: Cue) -> str:
        if not self.cues:
            return ""
        i = bisect.bisect_left(self.starts, cue.start)
        for j in (i, i - 1):
            if 0 <= j < len(self.cues) and abs(self.cues[j].start - cue.start) <= 100:
                return self.cues[j].text
        # Ellers: saml alle engelske replikker der overlapper mindst halvdelen.
        found = []
        j = max(0, i - 3)
        while j < len(self.cues) and self.cues[j].start < cue.end:
            c = self.cues[j]
            overlap = min(c.end, cue.end) - max(c.start, cue.start)
            if overlap > 0 and overlap >= 0.5 * min(c.end - c.start, cue.end - cue.start):
                found.append(c.text)
            j += 1
        return "\n".join(found)


def check_all(
    da_cues: list[Cue], en_index: EnglishIndex, max_line_chars: int, bad_words: frozenset[str] | None = None
) -> dict[int, list[str]]:
    """Returnerer {position (0-baseret): [flag]} for replikker med fejl."""
    result: dict[int, list[str]] = {}
    for pos, cue in enumerate(da_cues):
        flags = check_cue(cue.text, en_index.text_for(cue), max_line_chars, bad_words)
        if flags:
            result[pos] = flags
    return result
