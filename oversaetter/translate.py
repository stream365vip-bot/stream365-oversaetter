"""Bed Bazarr oversætte og vent på at den danske fil dukker op.

Bazarrs translate-kald (PATCH /api/subtitles action=translate) lægger i nyere versioner
oversættelsen i Bazarrs jobkø og svarer 204 med det samme. I ældre versioner blokerer
kaldet, til oversættelsen er færdig. Vi håndterer begge: kaldet får lov at vare lige så
længe som TRANSLATE_TIMEOUT_MINUTES, og bagefter venter vi på, at <video>.da.srt opstår
eller ændres. Hvis Bazarrs jobkø kan læses, bruges den til at opdage fejl tidligt.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx2 as httpx

from .selection import Candidate
from .srt import read_file

log = logging.getLogger("oversaetter")

POLL_SECONDS = 15
JOB_GRACE_SECONDS = 60


@dataclass
class Outcome:
    ok: bool
    permanent: bool = False  # True: prøv ikke igen (fx billedbaseret spor)
    reason: str = ""


def _mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _is_translate_job(job: dict) -> bool:
    return "ranslat" in str(job.get("job_name", ""))


class _FileWatch:
    def __init__(self, path: str):
        self.path = path
        self.before = _mtime(path)
        self.last_size: int | None = None

    def ready(self) -> bool:
        """Filen er ny/ændret, har samme størrelse som ved sidste kig og kan læses."""
        mtime = _mtime(self.path)
        if mtime is None or (self.before is not None and mtime <= self.before):
            return False
        size = os.path.getsize(self.path)
        stable = size == self.last_size and size > 0
        self.last_size = size
        if not stable:
            return False
        try:
            return len(read_file(self.path)) > 0
        except OSError:
            return False


def request_translation(
    bazarr, cand: Candidate, timeout_minutes: float, sleep=time.sleep, clock=time.monotonic
) -> Outcome:
    watch = _FileWatch(cand.da_path)
    jobs_before = bazarr.jobs()
    known = {j.get("job_id") for j in jobs_before} if jobs_before is not None else None
    deadline = clock() + timeout_minutes * 60

    try:
        r = bazarr.translate(
            cand.media_type,
            cand.media_id,
            max(30.0, deadline - clock()),
            path=cand.source_path,
            subtitles_id=cand.subtitles_id,
        )
    except httpx.TimeoutException:
        log.info("Bazarr svarede ikke inden for tidsgrænsen - tjekker om filen alligevel er kommet")
        r = None
    except httpx.HTTPError as e:
        return Outcome(False, reason=f"kunne ikke kontakte Bazarr: {e}")

    if r is not None and r.status_code not in (200, 204):
        text = (r.text or "").strip()[:200]
        if r.status_code == 415:
            return Outcome(
                False, permanent=True, reason="billedbaseret undertekstspor (PGS/VobSub) kan ikke oversættes"
            )
        if r.status_code == 501 and cand.subtitles_id is not None:
            return Outcome(
                False, permanent=True, reason="denne Bazarr-version kan ikke oversætte indlejrede spor (subtitles_id)"
            )
        if r.status_code == 400:
            return Outcome(False, permanent=True, reason=f"Bazarr afviste kaldet: {text}")
        return Outcome(False, reason=f"Bazarr svarede {r.status_code}: {text}")

    ended_at: float | None = None
    while True:
        if watch.ready():
            return Outcome(True)
        if clock() >= deadline:
            return Outcome(False, reason=f"ingen dansk fil efter {timeout_minutes:g} minutter")
        if known is not None:
            jobs = bazarr.jobs() or []
            mine = [j for j in jobs if j.get("job_id") not in known and _is_translate_job(j)]
            if mine and all(j.get("status") in ("failed", "completed") for j in mine):
                if any(j.get("status") == "failed" for j in mine):
                    if not os.path.exists(cand.da_path) or _mtime(cand.da_path) == watch.before:
                        return Outcome(False, reason="Bazarrs oversættelsesjob fejlede (se Bazarrs log)")
                ended_at = ended_at or clock()
                if clock() - ended_at > JOB_GRACE_SECONDS:
                    return Outcome(False, reason="Bazarrs job sluttede uden at skrive en dansk fil")
        sleep(POLL_SECONDS)
