"""Gyldige søgninger: tjenestestatus i Bazarr og venten på Bazarrs søgejob.

En søgning er kun gyldig, hvis ingen af Bazarrs aktiverede undertekst-tjenester var sat på pause
(throttled, nede, kvote overskredet) hverken lige før eller lige efter søgningen. Der oversættes aldrig
uden en gyldig søgning.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

PAUSE_WARN = timedelta(hours=24)
POLL_SECONDS = 10
# Bazarrs interne navne -> navne som i Bazarrs brugerflade
NICE_NAMES = {
    "opensubtitlescom": "OpenSubtitles",
    "opensubtitles": "OpenSubtitles.org",
    "podnapisi": "Podnapisi",
    "subdl": "SubDL",
    "addic7ed": "Addic7ed",
    "embeddedsubtitles": "Embedded Subtitles",
    "subf2m": "Subf2m",
    "yifysubtitles": "YIFY Subtitles",
    "gestdown": "Gestdown",
    "supersubtitles": "SuperSubtitles",
    "subsource": "SubSource",
    "animetosho": "AnimeTosho",
    "jimaku": "Jimaku",
}
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}
_WORDS = {
    "in a minute": 60,
    "in an hour": 3600,
    "tomorrow": 86400,
    "day after": 2 * 86400,
    "next week": 7 * 86400,
    "next month": 30 * 86400,
}


def nice_name(name: str) -> str:
    return NICE_NAMES.get(name.lower(), name)


@dataclass
class Paused:
    name: str
    status: str  # Bazarrs grund, fx "TooManyRequests"
    retry: str  # Bazarrs tekst, fx "in 2 hours"

    def until(self, now: datetime | None = None) -> str:
        """ "til ca. kl. 14:30" ud fra Bazarrs omtrentlige "in 2 hours". Tom tekst hvis tiden er ukendt."""
        text = (self.retry or "").strip().lower()
        seconds = _WORDS.get(text)
        m = re.fullmatch(r"in (\d+) (second|minute|hour|day|week)s?", text)
        if m:
            seconds = int(m.group(1)) * _UNITS[m.group(2)]
        if seconds is None:
            return f"(Bazarr: {self.retry})" if text and text != "-" else ""
        now = now or datetime.now().astimezone()
        at = now + timedelta(seconds=seconds)
        return f"til ca. kl. {at:%H:%M}" if at.date() == now.date() else f"til ca. {at:%d.%m. kl. %H:%M}"

    def describe(self, now: datetime | None = None) -> str:
        return " ".join(x for x in (f"{nice_name(self.name)} er sat på pause", self.until(now)) if x)


def paused_providers(bazarr) -> list[Paused]:
    """Aktiverede tjenester, der er sat på pause lige nu. ApiError hvis status ikke kan læses."""
    out = []
    for p in bazarr.providers():
        status = str(p.get("status") or "Good")
        if status.lower() != "good":
            out.append(Paused(str(p.get("name") or "?"), status, str(p.get("retry") or "")))
    return out


def waiting_text(paused: list[Paused], now: datetime | None = None) -> str:
    return "venter: " + ", ".join(p.describe(now) for p in paused)


def search_jobs(jobs: list[dict] | None) -> dict[int, str]:
    """job_id -> status for Bazarrs søgejob ("Searching subtitles" / "Searching DA for ...")."""
    return {
        int(j["job_id"]): str(j.get("status"))
        for j in jobs or []
        if j.get("job_id") is not None and "earching" in str(j.get("job_name", ""))
    }


def unfinished(bazarr, job_ids: list[int]) -> list[int] | None:
    """De af job_ids der stadig venter eller kører. None hvis jobkøen ikke kan læses.

    Bazarr gemmer kun de sidste 10 færdige job, så et job der er væk fra listen, regnes som færdigt.
    """
    jobs = bazarr.jobs()
    if jobs is None:
        return None
    status = {int(j["job_id"]): str(j.get("status")) for j in jobs if j.get("job_id") is not None}
    return [i for i in job_ids if status.get(i) in ("pending", "running")]


def wait_for_jobs(bazarr, job_ids: list[int], timeout_s: float, sleep=time.sleep, clock=time.monotonic) -> list[int]:
    """Vent til søgejobbene er færdige, højst timeout_s. Returnerer de job, der stadig ikke er færdige."""
    deadline = clock() + timeout_s
    left = list(job_ids)
    while left:
        now_left = unfinished(bazarr, left)
        if now_left is not None:
            left = now_left
            if not left:
                break
        if clock() >= deadline:
            break
        sleep(POLL_SECONDS)
    return left
