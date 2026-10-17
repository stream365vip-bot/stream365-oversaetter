"""Kreditlinje: erstat Bazarrs '# Subtitles translated with ... #' med vores egen tekst."""

from __future__ import annotations

from .checks import is_bazarr_header
from .srt import Cue

CREDIT_START_MS = 1000
CREDIT_MAX_MS = 5000
GAP_BEFORE_FIRST_MS = 500
MIN_DURATION_MS = 2000


def credit_lines(credit_text: str) -> str:
    """Gør \\n (backslash + n) fra .env til rigtige linjeskift og fjern tomme linjer."""
    lines = (line.strip() for line in credit_text.replace("\\n", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


def split_header(cues: list[Cue], credit_text: str = "") -> tuple[list[Cue], bool]:
    """Fjern Bazarrs kreditreplik (den ligger blandt de første replikker).

    En kreditlinje fra os selv (fx i en fil, der allerede er efterbehandlet) fjernes også,
    så kontrollen og korrekturen aldrig ser den.
    """
    own = credit_lines(credit_text)
    for i, cue in enumerate(cues[:3]):
        if is_bazarr_header(cue) or (own and cue.text.strip() == own):
            return cues[:i] + cues[i + 1 :], True
    return list(cues), False


def apply_credit(cues: list[Cue], credit_text: str, had_header: bool = False) -> tuple[list[Cue], str]:
    """Returnerer (nye replikker, beskrivelse). Replikkerne nummereres om ved skrivning.

    had_header=True betyder, at Bazarrs kreditlinje allerede er fjernet af kalderen.
    """
    body, found = split_header(cues, credit_text)
    had_header = had_header or found
    credit_text = credit_lines(credit_text)
    if not credit_text:
        return body, "kreditlinje slået fra"
    first_start = min((c.start for c in body), default=None)
    end = CREDIT_START_MS + CREDIT_MAX_MS
    if first_start is not None:
        end = min(end, first_start - GAP_BEFORE_FIRST_MS)
    if end - CREDIT_START_MS < MIN_DURATION_MS:
        what = "Bazarrs kreditlinje fjernet, " if had_header else ""
        return body, what + "for lidt plads før første replik, kreditlinje udeladt"
    credit = Cue(start=CREDIT_START_MS, end=end, text=credit_text)
    what = "Bazarrs kreditlinje erstattet" if had_header else "kreditlinje indsat"
    return [credit] + body, what
