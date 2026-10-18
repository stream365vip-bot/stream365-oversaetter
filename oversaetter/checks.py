"""Kontrol A: gratis tjek af hver dansk replik mod den engelske kilde."""

from __future__ import annotations

import bisect
import functools
import re

from .hi import speaker_prefix
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

# Fyldlyde, der udelades i v4. En replik med kun fyldlyde får tom tekst og fjernes fra filen.
# Også de rene interjektioner (oh, huh, ooh): en tom oversættelse af dem udløser intet genforsøg (trin B2).
FILLER = {
    "uh",
    "uhh",
    "um",
    "umm",
    "uhm",
    "hm",
    "hmm",
    "hmmm",
    "mm",
    "mmm",
    "ah",
    "ahh",
    "aah",
    "ugh",
    "er",
    "erm",
    "oh",
    "ohh",
    "huh",
    "ooh",
}


def only_filler(text: str) -> bool:
    """Replikken består kun af fyldlyde (uh, um, hmm ...) og tegnsætning."""
    words = [w.lower() for w in WORD_RE.findall(plain(text))]
    rest = re.sub(r"[\W_]", "", WORD_RE.sub("", plain(text)))
    return bool(words) and all(w in FILLER for w in words) and not re.search(r"\d", rest)


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
    "compound_space": "a compound noun is written as two words (e.g. 'bil nøgle' -> 'bilnøgle')",
    "generic_you": "generic English 'you' translated as 'du' - use 'man' (e.g. 'Man ved aldrig')",
    "capitalized_weekday": "weekday or month with a capital letter mid-sentence (write it in lower case)",
    "english_word": "contains an English word",
    "reading_speed": "too many characters per second on screen - make it shorter",  # kun information
    "inconsistent_term": "a glossary term is translated differently from the film glossary",
    "mixed_case": "a made-up word with a capital letter inside it (e.g. 'sundhedsTHC') - translate the meaning",
    "hi_rest": "contains [brackets] or a speaker name like 'RICK:' from the English source - remove them",
}


# Informationsflag tælles i rapporten og loggen, men sendes aldrig i rettelsesrunden (trin B2, 18.10.2026).
# reading_speed: på korte replikker er det oftest filmens timing, ikke oversættelsen, der er problemet.
INFO_FLAGS = frozenset({"reading_speed"})


def fix_flags(flags: dict[int, list[str]]) -> dict[int, list[str]]:
    """Kun de flag, der skal rettes: informationsflag fjernes, og replikker med kun informationsflag udelades."""
    out = {}
    for pos, fl in flags.items():
        keep = [f for f in fl if f not in INFO_FLAGS]
        if keep:
            out[pos] = keep
    return out


def count_info(flags: dict[int, list[str]]) -> int:
    """Antal replikker med mindst ét informationsflag."""
    return sum(1 for fl in flags.values() if any(f in INFO_FLAGS for f in fl))


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


def check_cue(
    da: str,
    en: str,
    max_line_chars: int,
    bad_words: frozenset[str] | None = None,
    duration_ms: int = 0,
    glossary=None,
    strict: bool = False,
    reading_speed_max: float | None = None,
) -> list[str]:
    """Flag for én replik. strict=True (PROMPT_VERSION=v4 og oversaet-test) giver også de skarpere flag."""
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
    if hi_rest(da):
        flags.append("hi_rest")
    if en:
        da_nums = _numbers(da)
        da_digits = re.sub(r"\D", "", plain(da))
        if any(n not in da_nums and n not in da_digits for n in _numbers(en)):
            flags.append("missing_number")
        if (
            _norm(da) == _norm(en)
            and _norm(da)
            and not _is_names_or_interjections(en)
            and not same_is_fine(en, glossary)
        ):
            flags.append("identical_to_english")
    if strict:
        if compound_space(da):
            flags.append("compound_space")
        if en and generic_you(da, en):
            flags.append("generic_you")
        if capitalized_weekday(da):
            flags.append("capitalized_weekday")
        if english_word(da, en, glossary) and "identical_to_english" not in flags:
            flags.append("english_word")
        if reading_speed(da, en, duration_ms, reading_speed_max or MAX_CPS):
            flags.append("reading_speed")
        if en and inconsistent_term(da, en, glossary):
            flags.append("inconsistent_term")
        if mixed_case(da):
            flags.append("mixed_case")
    return flags


# --- skarpere kontrol (18.10.2026). Konservativ: hellere et flag for lidt end en falsk alarm. -------------
# Sammensætninger, der aldrig er rigtige som to ord. Andet ord må have en dansk endelse (nøglen, vagterne ...).
COMPOUNDS = [
    ("bil", "nøgle"),
    ("hoved", "nøgle"),
    ("nat", "syn"),
    ("nattesyns", "brille"),
    ("tele", "fon"),
    ("under", "tekst"),
    ("sikkerheds", "vagt"),
    ("politi", "station"),
    ("politi", "bil"),
    ("hoved", "kvarter"),
    ("fødsels", "dag"),
    ("køle", "skab"),
    ("mobil", "telefon"),
    ("kredit", "kort"),
    ("computer", "skærm"),
    ("sove", "værelse"),
    ("bade", "værelse"),
    ("skole", "lærer"),
    ("dør", "klokke"),
    ("brand", "mand"),
]
_ENDINGS = r"(?:ernes|erne|rnes|rne|ens|ets|en|et|er|ne|n|r|s|e)?"
COMPOUND_RE = re.compile(
    r"(?<![\wæøå])(?:" + "|".join(rf"{a} {b}{_ENDINGS}" for a, b in COMPOUNDS) + r")(?![\wæøå])",
    re.IGNORECASE,
)
# "under tekst" er også en præposition + navneord ("under teksten står ...") - kun når det står først.
UNDER_TEKST_OK_RE = re.compile(r"\bunder tekst", re.IGNORECASE)

GENERIC_YOU_EN_RE = re.compile(r"^\W*you\s+(never|can't|cannot|can never|just)\b", re.IGNORECASE)
GENERIC_YOU_DA_RE = re.compile(r"^\W*(du ved aldrig|du kan aldrig|du skal bare)\b", re.IGNORECASE)
# Ord der viser, at replikken er henvendt til en bestemt person.
ADDRESS_WORDS = {
    "man",
    "dude",
    "buddy",
    "honey",
    "sweetie",
    "guys",
    "sir",
    "ma'am",
    "kid",
    "baby",
    "bro",
    "mom",
    "dad",
    "mister",
    "pal",
    "darling",
    "son",
    "girl",
    "boy",
}

WEEKDAYS_MONTHS = {
    "mandag",
    "tirsdag",
    "onsdag",
    "torsdag",
    "fredag",
    "lørdag",
    "søndag",
    "januar",
    "februar",
    "marts",
    "april",
    "juni",
    "juli",
    "september",
    "oktober",
    "november",
    "december",
}  # "maj" og "august" er udeladt: de kan også være navne eller stå i titler
SENTENCE_START = '.!?-:"…♪(['

# Engelske ord, der aldrig hører hjemme i en dansk replik (hele ord). "and" (en and) og "her" er danske ord.
ENGLISH_ONLY = {
    "the",
    "with",
    "what",
    "please",
    "check",
    "swag",
    "healthcare",
    "whatever",
    "you",
    "your",
    "yeah",
    "thanks",
    "they",
    "this",
    "that",
    "would",
    "could",
    "should",
    "because",
    "really",
    "something",
    "nothing",
    "everything",
    "guys",
    "anyway",
    # slang, bandeord og tiltaler (trin B2: blev stående uden flag i test14)
    "bitch",
    "bitches",
    "dude",
    "nobody",
    "nobodies",
    "crew",
    "bossman",
    "boss",
    "mr",
    "mrs",
    "miss",
    "sir",
    "ma'am",
    "buddy",
    "girl",
    # udbrud, skældsord og kæleord (trin B3: blev stående uden flag i test16)
    "damn",
    "gosh",
    "jeez",
    "seriously",
    "literally",
    "babe",
    "sweetie",
    "honey",
    "bastard",
    "asshole",
    "jerk",
    "loser",
    "losers",
}
# Engelske ord, der kun flages i bestemte vendinger: "shit" alene er tilladt, "holy shit" er ikke.
ENGLISH_PHRASE_RE = re.compile(r"\bholy\s+shit\b|\bhell\s*!|\bwhat\s+the\s+hell\b", re.IGNORECASE)
DANISH_APOSTROPHE_ENDINGS = {"en", "et", "s", "er", "erne", "ene", "ens", "ets"}
# Titler flages også, når ordlisten har navnet uændret ("Mr. Vice" -> "Mr. Vice" skal være "hr. Vice").
TITLES = {"mr", "mrs", "miss", "sir", "ma'am"}
# "Man" som engelsk udbrud: stort M og ikke efterfulgt af et ord ("Man!", "Man, det er koldt."). "Man ved aldrig"
# og ", man" er dansk og må stå.
ENGLISH_MAN_RE = re.compile(r"(?<![\wæøåÆØÅ'])Man(?=\s*(?:[,!?.…]|$))", re.MULTILINE)
# Ord der må stå (låneord og ord, der er almindelige i dansk tale).
ENGLISH_ALLOWED = ALLOWED_LOANWORDS | {"sorry", "awesome", "weird", "fair", "deal", "date", "party", "vibe"}

# Enkelte ord, der er ens på dansk og engelsk, så en ens replik ikke er en fejl.
SAME_WORDS = {
    "stop",
    "okay",
    "ok",
    "hey",
    "wow",
    "yo",
    "cool",
    "nice",
    "shit",
    "fuck",
    "bro",
    "taxi",
    "hotel",
    "pizza",
    "bar",
    "film",
    "sex",
    "super",
    "alarm",
    "radio",
    "video",
    "show",
    "job",
    "team",
    "chef",
    "hallo",
    "halløj",
    "mor",
    "far",
    "nej",
    "ja",
}
FOREIGN_LETTERS_RE = re.compile(r"[¿¡ñçàèêëîïôûùœ]", re.IGNORECASE)
FOREIGN_WORDS = {
    "je",
    "tu",
    "est",
    "le",
    "les",
    "c'est",
    "oui",
    "non",
    "merci",
    "monsieur",
    "madame",
    "mademoiselle",
    "bonjour",
    "bonsoir",
    "pourquoi",
    "mais",
    "très",
    "señor",
    "señora",
    "sí",
    "gracias",
    "por",
    "favor",
    "qué",
    "que",
    "el",
    "los",
    "las",
    "usted",
    "hola",
    "amigo",
    "adiós",
    "vamos",
    "dios",
    "mío",
    "danke",
    "bitte",
    "nein",
    "ich",
    "nicht",
    "grazie",
    "prego",
    "ciao",
    "signore",
    "da",
    "niet",
    "spasibo",
}
MAX_CPS = 25.0  # standard for READING_SPEED_MAX (hævet fra 20 i trin B2)


def _glossary_terms(glossary) -> list[dict]:
    return list(getattr(glossary, "terms", None) or [])


def _glossary_names(glossary) -> set[str]:
    """Ord fra ordlistens termer, der står uændret på dansk (navne)."""
    out: set[str] = set()
    for t in _glossary_terms(glossary):
        if t["en"].strip().lower() == t["da"].strip().lower():
            out.update(_words(t["en"]))
    return out


def is_foreign(en: str) -> bool:
    """Replikken er på et andet fremmedsprog end engelsk (fransk, spansk ...)."""
    words = _words(en)
    if not words:
        return False
    if any(w in ENGLISH_WORDS and w not in DANISH_HOMOGRAPHS and w not in FOREIGN_WORDS for w in words):
        return False
    return bool(FOREIGN_LETTERS_RE.search(plain(en))) or any(w in FOREIGN_WORDS for w in words)


def same_is_fine(en: str, glossary=None) -> bool:
    """En dansk replik, der er identisk med den engelske, er i orden for navne, tal, ens ord og fremmedsprog."""
    words = _words(en)
    if not words:
        return True  # kun tal og tegn
    names = _glossary_names(glossary)
    if all(w in names or w in INTERJECTIONS for w in words):
        return True
    if len(words) == 1 and words[0] in SAME_WORDS:
        return True
    return is_foreign(en)


def compound_space(da: str) -> bool:
    p = plain(da)
    for m in COMPOUND_RE.finditer(p):
        if UNDER_TEKST_OK_RE.match(m.group(0)):
            before = p[: m.start()].rstrip()
            if before and before[-1] not in SENTENCE_START:
                continue  # "står under teksten" - en præposition
        return True
    return False


def generic_you(da: str, en: str) -> bool:
    if not (GENERIC_YOU_EN_RE.search(plain(en)) and GENERIC_YOU_DA_RE.search(plain(da))):
        return False
    raw = WORD_RE.findall(plain(en))
    if any(w.lower() in ADDRESS_WORDS for w in raw):
        return False
    if "," in plain(en):  # "You never listen, Tom." - ofte en tiltale
        return False
    return not any(w[0].isupper() and w.lower() not in NOT_NAMES and w.lower() != "i" for w in raw[1:])


def capitalized_weekday(da: str) -> bool:
    p = plain(da)
    for m in WORD_RE.finditer(p):
        w = m.group(0)
        if w.lower() not in WEEKDAYS_MONTHS or not w[0].isupper():
            continue
        line_before = p[: m.start()].split("\n")[-1].rstrip(" \t")
        if line_before and line_before[-1] not in SENTENCE_START:
            return True
    return False


def _proper_names(glossary) -> list[str]:
    """Ordlisteudtryk, der er egennavne: samme engelske og danske form og stort begyndelsesbogstav (trin B3)."""
    out = []
    for t in _glossary_terms(glossary):
        en = t["en"].strip()
        if en and en == t["da"].strip() and en[0].isupper():
            out.append(en)
    return sorted(out, key=len, reverse=True)


def english_word(da: str, en: str = "", glossary=None) -> bool:
    text = plain(da)
    if ENGLISH_PHRASE_RE.search(text):
        return True
    # Egennavne fra ordlisten fjernes før tjekket ("The Paragon"), men titler i dem bliver stående ("Mr. Vice").
    for name in _proper_names(glossary):
        titles = " ".join(w for w in WORD_RE.findall(name) if w.lower() in TITLES)
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", f" {titles} ", text)
    for w in _words(text):
        base, _, ending = w.partition("'")
        if ending in DANISH_APOSTROPHE_ENDINGS:
            w = base  # "crew'et", "bitch'en"
        if w in ENGLISH_ONLY and w not in ENGLISH_ALLOWED:
            return True
    return bool(ENGLISH_MAN_RE.search(text))


def reading_speed(da: str, en: str, duration_ms: int, max_cps: float = MAX_CPS) -> bool:
    """Over max_cps tegn pr. sekund - men kun når den danske tekst ikke allerede er kortere end den engelske."""
    if duration_ms <= 0:
        return False
    text = plain(da).replace("\n", " ").strip()
    if len(text) / (duration_ms / 1000) <= max_cps:
        return False
    return len(text) >= len(plain(en).replace("\n", " ").strip())


def _stems(word: str) -> set[str]:
    """De første 4 bogstaver af ordet - også uden det tabte e (regel/reglen/reglerne/regler regnes som ens)."""
    w = word.lower()
    forms = {w}
    m = re.match(r"^(.{2,}?)e([lnr])(\w*)$", w)
    if m:
        forms.add(m.group(1) + m.group(2) + m.group(3))
    return {f[:4] for f in forms}


def inconsistent_term(da: str, en: str, glossary=None) -> bool:
    """Et ordlisteterm i den engelske replik er oversat anderledes end ordlistens danske form.

    Trin B3: en bøjning tæller som den samme form (samme 4 første bogstaver), og et ord flages kun én gang pr.
    replik, så gentagelse inden for replikken aldrig giver et flag.
    """
    da_stems = set().union(*(_stems(w) for w in _words(da))) if _words(da) else set()
    en_low = plain(en).lower()
    for t in _glossary_terms(glossary):
        src, dst = t["en"].lower(), t["da"].lower()
        if len(src) < 3 or src == dst:
            continue  # navne, der står uændret, tjekkes ikke her
        if not re.search(rf"(?<![\w]){re.escape(src)}(?![\w])", en_low):
            continue
        words = _words(dst)
        long_words = [w for w in words if len(w) >= 4] or words
        if not all(_stems(w) & da_stems for w in long_words):
            return True
    return False


MIXED_CASE_RE = re.compile(r"[a-zæøåäöüé][A-ZÆØÅÄÖÜ]")


def mixed_case(da: str) -> bool:
    """Et opfundet ord med stort bogstav inde i ordet efter små ("sundhedsTHC").

    Fritaget: ord, der starter med stort (navne som OnlyFans, YouTube, og forkortelser som THC, JD's), ord med ét
    stort bogstav som andet tegn (iPhone, eBay) og bindestreg før det store bogstav (sundheds-THC).
    """
    for w in WORD_RE.findall(plain(da)):
        if w[0].isupper():
            continue
        if sum(c.isupper() for c in w) == 1 and w[1:2].isupper():
            continue
        if MIXED_CASE_RE.search(w):
            return True
    return False


def hi_rest(da: str) -> bool:
    """Rester fra en HI-kilde i den danske tekst: [klammer] (fx talernoten "[Rick]") eller "NAVN:" først i en linje."""
    if "[" in da or "]" in da:
        return True
    return any(speaker_prefix(line) for line in da.split("\n"))


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
    da_cues: list[Cue],
    en_index: EnglishIndex,
    max_line_chars: int,
    bad_words: frozenset[str] | None = None,
    glossary=None,
    strict: bool = False,
    reading_speed_max: float | None = None,
) -> dict[int, list[str]]:
    """Returnerer {position (0-baseret): [flag]} for replikker med fejl (inkl. informationsflag, se INFO_FLAGS)."""
    result: dict[int, list[str]] = {}
    for pos, cue in enumerate(da_cues):
        en = en_index.text_for(cue)
        flags = check_cue(
            cue.text, en, max_line_chars, bad_words, cue.end - cue.start, glossary, strict, reading_speed_max
        )
        if flags:
            result[pos] = flags
    return result
