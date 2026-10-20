"""Trin B6b: efter upload - vent på Bazarrs egen synk, tjek HI-mærket, og (valgfrit) bed selv om synk.

Bazarr starter selv en synkronisering (ffsubsync) lige efter vores upload (set i Bazarrs log på The Fix og Clean
Hands). Derfor venter vi på Bazarrs job for titlen ("Uploading/Syncing/Synced <fil>"), højst BAZARR_WAIT, og tjekker
først derefter, hvordan Bazarr har registreret den danske tekst: er den HI, logges en ADVARSEL med filnavnet.

SYNC_AFTER_UPLOAD=true (standard false) beder desuden selv om det samme som Sync-knappen (PATCH /api/subtitles,
action=sync) med forced=False og hi=False - kun til, hvis Bazarr holder op med at synkronisere ved upload. Fejl eller
timeout bliver til en tydelig advarsel; emnet fejler aldrig af den grund, og Plex får besked bagefter alligevel.
"""

from __future__ import annotations

import logging
import os

from .clients import ApiError, Bazarr
from .config import Config
from .hi import hi_filename
from .search import POLL_SECONDS
from .selection import Candidate

log = logging.getLogger("oversaetter")

BAZARR_WAIT = 600.0  # højst så mange sekunder på Bazarrs egen upload/synk, før HI-tjekket og Plex
SYNC_TIMEOUT = 600.0  # højst så mange sekunder på vores egen synk (kaldet og ventetiden på jobbet)
FAILED = ("failed", "error")
BUSY = ("pending", "running")


def danish_subtitle(bazarr: Bazarr, cand: Candidate) -> dict | None:
    """Den danske eksterne tekst, som Bazarr har registreret (helst vores egen <video>.da.srt)."""
    item = bazarr.item(cand.media_type, cand.media_id) or {}
    subs = [
        s
        for s in item.get("subtitles") or []
        if (s.get("code2") == "da" or str(s.get("code3") or "").lower() == "dan")
        and s.get("path")
        and not s.get("forced")
    ]
    name = os.path.basename(cand.da_path)
    subs.sort(key=lambda s: os.path.basename(str(s["path"])) != name)
    return subs[0] if subs else None


def _job_ids(jobs: list[dict] | None) -> set[int]:
    return {int(j["job_id"]) for j in jobs or [] if j.get("job_id") is not None}


def _title_jobs(jobs: list[dict], cand: Candidate) -> list[dict]:
    """Bazarrs job for titlen: navnet nævner videofilens navn (Uploading/Syncing/Synced <video>.da.srt)."""
    stem = os.path.splitext(os.path.basename(cand.video_path))[0].lower() + "."
    return [j for j in jobs if stem in str(j.get("job_name", "")).lower()]


def wait_for_bazarr_sync(bazarr: Bazarr, cand: Candidate, sleep, clock) -> bool | None:
    """Vent til Bazarrs egne job for titlen (upload og den automatiske synk) er færdige, højst BAZARR_WAIT.

    True = færdige (eller ingen job), False = timeout (advarsel), None = jobkøen kan ikke læses (gammel Bazarr).
    """
    deadline = clock() + BAZARR_WAIT
    while True:
        jobs = bazarr.jobs()
        if jobs is None:
            log.info("Bazarrs jobliste kan ikke læses - venter ikke på Bazarrs egen synk af %s", cand.title)
            return None
        busy = [j for j in _title_jobs(jobs, cand) if str(j.get("status")) in BUSY]
        if not busy:
            done = _title_jobs(jobs, cand)
            if done:
                log.info(
                    "Bazarr er færdig med upload/synk af den danske tekst til %s (%s)",
                    cand.title,
                    str(done[-1].get("job_name", "")),
                )
            return True
        if clock() >= deadline:
            log.warning(
                "ADVARSEL: Bazarrs egen synk af den danske tekst til %s er ikke færdig efter %d minutter (%s) - "
                "tjekker HI og beder Plex scanne alligevel",
                cand.title,
                BAZARR_WAIT // 60,
                str(busy[0].get("job_name", "")),
            )
            return False
        sleep(POLL_SECONDS)


def check_registered_hi(bazarr: Bazarr, cand: Candidate) -> dict | None:
    """Slå op, hvordan Bazarr har registreret den danske tekst. ADVARSEL med filnavnet, hvis den er HI."""
    try:
        sub = danish_subtitle(bazarr, cand)
    except ApiError as e:
        log.warning("Kunne ikke slå den danske tekst til %s op i Bazarr efter upload: %s", cand.title, e)
        return None
    if sub and (sub.get("hi") or hi_filename(str(sub["path"]))):
        log.warning(
            "ADVARSEL: Bazarr har registreret den danske tekst til %s som HI, gemt som %s (%s), selv om den blev "
            "uploadet med hi=false - tjek filen i Bazarr",
            cand.title,
            os.path.basename(str(sub["path"])),
            sub["path"],
        )
    return sub


def after_upload(cfg: Config, bazarr: Bazarr, cand: Candidate, sleep, clock) -> bool | None:
    """Efter en vellykket upload, før Plex: vent på Bazarrs synk, tjek HI, og (SYNC_AFTER_UPLOAD) synk selv.

    Returnerer resultatet af vores egen synk: True/False, eller None når den er slået fra.
    """
    wait_for_bazarr_sync(bazarr, cand, sleep, clock)
    sub = check_registered_hi(bazarr, cand)
    return sync_after_upload(cfg, bazarr, cand, sleep, clock, str(sub["path"]) if sub else cand.da_path)


def sync_after_upload(cfg: Config, bazarr: Bazarr, cand: Candidate, sleep, clock, path: str) -> bool | None:
    """True = synkroniseret, False = fejlede eller timeout (advarsel), None = slået fra (SYNC_AFTER_UPLOAD=false)."""
    if not cfg.sync_after_upload:
        log.debug("SYNC_AFTER_UPLOAD=false - Bazarr synkroniserer selv ved upload (%s)", cand.title)
        return None
    deadline = clock() + SYNC_TIMEOUT
    before = bazarr.jobs()
    try:
        bazarr.sync_subtitle(cand.media_type, cand.media_id, path, max(1.0, deadline - clock()))
    except ApiError as e:
        log.warning(
            "ADVARSEL: Bazarr kunne ikke synkronisere den danske tekst til %s: %s - teksten er uploadet, men ikke "
            "synkroniseret (Plex får besked alligevel)",
            cand.title,
            e,
        )
        return False
    # Bazarr opretter synkjobbet, før det svarer. Intet nyt job (eller ingen jobkø): svaret er hele synken.
    after = bazarr.jobs() if before is not None else None
    fresh = [j for j in after or [] if j.get("job_id") is not None and int(j["job_id"]) not in _job_ids(before)]
    fresh = [j for j in fresh if "ync" in str(j.get("job_name", ""))] or fresh
    job = int(fresh[0]["job_id"]) if fresh else None
    while job is not None:
        # Ulæselig jobkø: vent videre. Jobbet væk fra listen (Bazarr gemmer kun de sidste færdige): færdigt.
        status = "pending" if after is None else "completed"
        for j in after or []:
            if j.get("job_id") is not None and int(j["job_id"]) == job:
                status = str(j.get("status"))
        if status in FAILED:
            log.warning(
                "ADVARSEL: Bazarrs synk af den danske tekst til %s fejlede (job %s: %s) - teksten er uploadet, men "
                "ikke synkroniseret (Plex får besked alligevel)",
                cand.title,
                job,
                status,
            )
            return False
        if status not in BUSY:
            break
        if clock() >= deadline:
            log.warning(
                "ADVARSEL: Bazarr har ikke synkroniseret den danske tekst til %s inden for %d minutter (job %s kører "
                "stadig) - Plex får besked alligevel",
                cand.title,
                SYNC_TIMEOUT // 60,
                job,
            )
            return False
        sleep(POLL_SECONDS)
        after = bazarr.jobs()
    log.info("Bazarr har synkroniseret den danske tekst til %s mod lyden (%s)", cand.title, path)
    return True
