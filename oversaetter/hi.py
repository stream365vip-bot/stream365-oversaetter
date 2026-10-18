"""Rensning af engelske hørehæmmede-tekster (HI/SDH) før oversættelsen (trin B4, 18.10.2026).

HI-tekster har lydbeskrivelser ([DOOR SLAMS], (whispers)), musiklinjer (♪ ... ♪) og talernavne (RICK: Let's go.).
Bazarrs Sub-Zero-rettelse fjerner noget af det fra hentede tekster, men ikke fra indlejrede spor, som vi selv trækker
ud med ffmpeg. Intet af det må stå i den danske tekst - men talernavnene hjælper oversættelsen (du/I, han/hun,
tiltale), så de gemmes og sendes til modellen som en note: "[Rick] Let's go.".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace

from .srt import Cue

log = logging.getLogger("oversaetter")

# Kendte lydbeskrivelser i (parenteser) eller [klammer]. Indholdet tæller, når det starter med et af udtrykkene.
SOUND_WORDS = (
    "applause",
    "barking",
    "beeping",
    "bell",
    "bells",
    "birds",
    "breathing",
    "breathes",
    "chatter",
    "chattering",
    "cheering",
    "cheers",
    "chuckles",
    "chuckling",
    "clapping",
    "clears throat",
    "coughs",
    "coughing",
    "crash",
    "crashing",
    "cries",
    "crying",
    "crowd",
    "dog",
    "door",
    "doorbell",
    "engine",
    "explosion",
    "exhales",
    "footsteps",
    "gasps",
    "gasping",
    "giggles",
    "giggling",
    "groans",
    "groaning",
    "grunts",
    "grunting",
    "gunfire",
    "gunshot",
    "gunshots",
    "hums",
    "humming",
    "in arabic",
    "in chinese",
    "in french",
    "in german",
    "in italian",
    "in japanese",
    "in korean",
    "in russian",
    "in spanish",
    "inaudible",
    "indistinct",
    "inhales",
    "knock",
    "knocking",
    "knocks",
    "laughs",
    "laughing",
    "laughter",
    "moans",
    "moaning",
    "mumbles",
    "mumbling",
    "music",
    "muttering",
    "mutters",
    "panting",
    "phone",
    "rings",
    "ringing",
    "screams",
    "screaming",
    "shouting",
    "shouts",
    "shrieks",
    "sighs",
    "sighing",
    "silence",
    "singing",
    "sings",
    "siren",
    "sirens",
    "sneezes",
    "sniffles",
    "sniffs",
    "snoring",
    "sobs",
    "sobbing",
    "speaking foreign language",
    "speaking",
    "static",
    "stammers",
    "stutters",
    "tires screech",
    "thud",
    "thunder",
    "upbeat music",
    "whimpers",
    "whispering",
    "whispers",
    "whistles",
    "yawns",
    "yells",
    "yelling",
)
_SOUND_RE = re.compile(r"^(?:" + "|".join(re.escape(w) for w in SOUND_WORDS) + r")(?![a-z])")
BRACKET_RE = re.compile(r"\[([^\[\]]*)\]|\(([^()]*)\)")
TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")
EMPTY_TAG_RE = re.compile(r"<(i|b|u)>(\s*)</\1>", re.IGNORECASE)
MUSIC = "♪#"

# "NAVN:" eller "- NAVN:" først i linjen: 1-3 ord med store bogstaver, mindst to bogstaver i alt.
_UPPER = "A-ZÀ-ÖØ-Þ"
_NAME_WORD = rf"[{_UPPER}][{_UPPER}0-9'.\-]*"
SPEAKER_RE = re.compile(
    rf"^(?P<pre>(?:\s*(?:<[^>]+>|\{{[^}}]*\}}))*\s*(?:-\s*)?(?:<[^>]+>)*)"
    rf"(?P<name>{_NAME_WORD}(?: {_NAME_WORD}){{0,2}}):(?:\s+|$)"
)

# Korte ord med kolon, der ikke er talernavne ("OK: fint", "TV: nyheder").
NOT_SPEAKERS = {"OK", "TV", "PS", "AM", "PM", "NO", "YES", "HEY", "NOTE"}


def speaker_prefix(line: str) -> re.Match | None:
    """Et talernavn som "RICK:" eller "- MAN ON RADIO:" først i linjen (med evt. tags og bindestreg før)."""
    m = SPEAKER_RE.match(line)
    if m is None or sum(c.isalpha() for c in m.group("name")) < 2 or m.group("name") in NOT_SPEAKERS:
        return None
    return m


@dataclass
class HiStats:
    sounds: int = 0
    music: int = 0
    speakers: int = 0
    dropped: int = 0

    def text(self) -> str:
        return (
            f"{self.sounds} lydbeskrivelser, {self.music} musiklinjer, {self.speakers} talernavne, "
            f"{self.dropped} replikker droppet"
        )

    def any(self) -> bool:
        return bool(self.sounds or self.music or self.speakers or self.dropped)


@dataclass
class HiResult:
    cues: list[Cue]  # de rensede replikker (tomme er droppet)
    notes: list[str]  # samme replikker med talernavnet som note ("[Rick] Let's go.") - kun til modellens input
    speakers: list[str] = field(default_factory=list)  # unikke talernavne i den rækkefølge, de optræder
    stats: HiStats = field(default_factory=HiStats)


def _plain(text: str) -> str:
    return TAG_RE.sub("", text)


def _is_sound(content: str) -> bool:
    """Indholdet af en klamme/parentes er en lydbeskrivelse: kun STORE bogstaver eller en kendt beskrivelse."""
    text = _plain(content).strip(" \t\n" + MUSIC)
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return not text  # "[]" og "[♪]" er lyd; "(2)" er ikke
    if not any(c.islower() for c in letters):
        return True
    return bool(_SOUND_RE.match(" ".join(text.lower().split())))


def _remove_sounds(text: str, stats: HiStats) -> str:
    def sub(m: re.Match) -> str:
        content = m.group(1) if m.group(1) is not None else m.group(2)
        if _is_sound(content):
            stats.sounds += 1
            return "\x00"
        return m.group(0)

    text = BRACKET_RE.sub(sub, text)
    text = re.sub(r"[ \t]*\x00[ \t\x00]*([,.!?…])", r"\1", text)  # "Wait (sighs), what?" -> "Wait, what?"
    return text.replace("\x00", " ")


def _is_music(line: str) -> bool:
    """Linjen består kun af ♪/# eller er en sanglinje markeret med ♪ ... ♪ (eller # ... #)."""
    p = _plain(line).strip()
    p = re.sub(r"^-\s*", "", p)
    if not p:
        return False
    if all(c in MUSIC or c.isspace() for c in p):
        return True
    return p[0] in MUSIC and p[-1] in MUSIC


def _tidy(line: str) -> str:
    line = EMPTY_TAG_RE.sub(r"\2", line)
    line = re.sub(r"[ \t]{2,}", " ", line).strip()
    line = re.sub(r"^((?:<[^/>][^>]*>)+)[ \t]+", r"\1", line)  # "<i> Come here." -> "<i>Come here."
    return re.sub(r"[ \t]+((?:</[^>]+>)+)$", r"\1", line)


def _name(raw: str) -> str:
    return raw.capitalize()  # "MAN ON RADIO" -> "Man on radio"


def _clean_cue(text: str, stats: HiStats) -> tuple[list[str], list[str | None]]:
    """Rens én replik. Returnerer (linjer, taler pr. linje)."""
    text = _remove_sounds(text, stats)
    lines = text.split("\n")
    whole = _plain(" ".join(lines)).strip()
    if len(lines) > 1 and whole and whole[0] in MUSIC and whole[-1] in MUSIC:
        stats.music += len([x for x in lines if _plain(x).strip()])
        lines = []  # sang over flere linjer: ♪ første linje / anden linje ♪
    out: list[str] = []
    speakers: list[str | None] = []
    carry = ""  # tags fra en droppet linje (fx "<i>"), der flyttes til næste linje
    dropped_line = False
    for line in lines:
        if _is_music(line):
            stats.music += 1
            carry += "".join(TAG_RE.findall(line))
            dropped_line = True
            continue
        line = _tidy(carry + line)
        carry = ""
        speaker = None
        m = speaker_prefix(line)
        if m is not None:
            speaker = _name(m.group("name"))
            stats.speakers += 1
            line = _tidy(m.group("pre") + line[m.end() :])
        if not _plain(line).strip(" -\t"):
            tags = "".join(TAG_RE.findall(line))
            if out and tags.startswith("</"):
                out[-1] += tags
            else:
                carry += tags
            dropped_line = True
            continue
        out.append(line)
        speakers.append(speaker)
    if carry and out:
        out[-1] += carry
    if out:
        out = [EMPTY_TAG_RE.sub(r"\2", x) for x in out]
    if dropped_line and len(out) == 1:
        out[0] = re.sub(r"^((?:<[^>]+>)*)-\s*", r"\1", out[0])  # kun én taler tilbage: ingen bindestreg
    return out, speakers


def note_line(line: str, speaker: str | None) -> str:
    """Linjen med talernavnet som note efter evt. tags og bindestreg: "- [Rick] Let's go."."""
    if not speaker:
        return line
    m = re.match(r"^((?:<[^>]+>|\{[^}]*\})*\s*(?:-\s*)?(?:<[^>]+>)*)", line)
    pre = m.group(1) if m else ""
    return f"{pre}[{speaker}] {line[len(pre) :]}"


def clean(cues: list[Cue]) -> HiResult:
    """Rens den engelske kilde. Logger tallene én gang, hvis der blev fjernet noget."""
    stats = HiStats()
    out: list[Cue] = []
    notes: list[str] = []
    names: dict[str, None] = {}
    for c in cues:
        lines, speakers = _clean_cue(c.text, stats)
        if not lines:
            stats.dropped += 1
            continue
        out.append(replace(c, text="\n".join(lines)))
        notes.append("\n".join(note_line(x, s) for x, s in zip(lines, speakers, strict=True)))
        names.update((s, None) for s in speakers if s)
    if stats.any():
        log.info("HI-rensning: %s", stats.text())
    return HiResult(out, notes, list(names), stats)


def passthrough(cues: list[Cue]) -> HiResult:
    """Ingen rensning (oversaet-test --hi off)."""
    return HiResult(list(cues), [c.text for c in cues])
