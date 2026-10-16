"""Lille SRT-læser/-skriver uden eksterne afhængigheder."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass

TIME_RE = re.compile(
    r"^\s*(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})(.*)$"
)


@dataclass
class Cue:
    start: int  # millisekunder
    end: int
    text: str  # linjer adskilt af "\n"
    extra: str = ""  # evt. positionsangivelse efter tidskoden

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n") if self.text else []


def _ms(h: str, m: str, s: str, frac: str) -> int:
    frac = (frac + "00")[:3]
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac)


def format_time(ms: int) -> str:
    ms = max(0, int(ms))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse(text: str) -> list[Cue]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").split("\n")
    cues: list[Cue] = []
    current: Cue | None = None
    body: list[str] = []

    def finish() -> None:
        if current is not None:
            while body and not body[-1].strip():
                body.pop()
            while body and not body[0].strip():
                body.pop(0)
            current.text = "\n".join(line.rstrip() for line in body)
            cues.append(current)

    i = 0
    while i < len(lines):
        line = lines[i]
        m = TIME_RE.match(line)
        # Et nummer tæller kun som replik-nummer, hvis det står efter en tom linje
        # (eller først i filen) og efterfølges af en tidskode.
        is_index = (
            line.strip().isdigit()
            and i + 1 < len(lines)
            and TIME_RE.match(lines[i + 1])
            and (i == 0 or not lines[i - 1].strip())
        )
        if is_index:
            i += 1
            m = TIME_RE.match(lines[i])
        if m:
            finish()
            g = m.groups()
            current = Cue(start=_ms(*g[0:4]), end=_ms(*g[4:8]), text="", extra=g[8].rstrip())
            body = []
        elif current is not None:
            body.append(line)
        i += 1
    finish()
    return cues


def compose(cues: list[Cue]) -> str:
    out = []
    for n, cue in enumerate(cues, start=1):
        out.append(f"{n}\n{format_time(cue.start)} --> {format_time(cue.end)}{cue.extra}\n{cue.text}\n")
    return "\n".join(out)


def read_file(path: str) -> list[Cue]:
    with open(path, "rb") as f:
        return parse(decode(f.read()))


def write_file_atomic(path: str, cues: list[Cue]) -> None:
    """Skriv til en midlertidig fil i samme mappe og omdøb den bagefter."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".oversaetter-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(compose(cues))
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
