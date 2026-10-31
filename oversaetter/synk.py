"""Trin B6b: bed Bazarr synkronisere vores danske tekst mod lyden, når den er uploadet.

Bazarrs automatiske synk kører kun, når Bazarr selv henter en tekst, ikke ved upload. Derfor beder vi om det samme
som Sync-knappen på filmens/afsnittets side (PATCH /api/subtitles, action=sync) med samme sprog, forced=False og
hi=False som uploaden, og stien som Bazarr selv har registreret den danske fil under. Nyere Bazarr lægger synken i
jobkøen; så ventes der på jobbet, højst SYNC_TIMEOUT i alt. Fejl eller timeout bliver til en tydelig advarsel - emnet
fejler aldrig af den grund, og Plex får besked bagefter alligevel (runneren).

Trin B8: registrerer Bazarr alligevel vores danske tekst som HI (fil .da.hi.srt), omdøbes filen til .da.srt (kun hvis
den ikke findes), Bazarr bedes scanne disken for emnet, og der tjekkes igen. En kopi af filen gemmes i
<DATA_DIR>/hi-fund/ (højst HI_FIND_MAX filer), så mønstret kan findes senere.
"""

from __future__ import annotations

import logging
import os
import shutil

from .clients import ApiError, Bazarr
from .config import Config
from .hi import hi_filename
from .search import POLL_SECONDS
from .selection import Candidate

log = logging.getLogger("oversaetter")

SYNC_TIMEOUT = 600.0  # højst så mange sekunder på hele synken (kaldet og ventetiden på jobbet)
FAILED = ("failed", "error")
HI_NOTE = "Bazarr har registreret den danske tekst som HI"  # kort tekst til feedet (trin B7)
HI_FIND_DIR = "hi-fund"  # under DATA_DIR (/data/hi-fund)
HI_FIND_MAX = 10
HI_RECHECK = 60.0  # så længe ventes der højst på, at Bazarr har scannet disken igen (sekunder)


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


def is_hi(sub: dict | None) -> bool:
    return bool(sub) and (bool(sub.get("hi")) or hi_filename(str(sub.get("path") or "")))


def sync_after_upload(cfg: Config, bazarr: Bazarr, cand: Candidate, sleep, clock) -> bool | None:
    """True = synkroniseret, False = fejlede eller timeout (advarsel), None = slået fra (SYNC_AFTER_UPLOAD=false)."""
    if not cfg.sync_after_upload:
        log.info("Synk efter upload er slået fra (SYNC_AFTER_UPLOAD=false) - %s synkroniseres ikke", cand.title)
        return None
    deadline = clock() + SYNC_TIMEOUT
    try:
        sub = danish_subtitle(bazarr, cand)
    except ApiError:
        sub = None
    path = str(sub["path"]) if sub else cand.da_path
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
        if status not in ("pending", "running"):
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


def _save_hi_find(cfg: Config, cand: Candidate, path: str) -> str | None:
    """Gem en kopi af den danske fil, som Bazarr mærkede HI, i <DATA_DIR>/hi-fund/ (højst HI_FIND_MAX filer)."""
    folder = os.path.join(cfg.data_dir, HI_FIND_DIR)
    try:
        os.makedirs(folder, exist_ok=True)
        if len(os.listdir(folder)) >= HI_FIND_MAX:
            log.info("HI-fund: %s har allerede %d filer - gemmer ikke en kopi af %s", folder, HI_FIND_MAX, cand.title)
            return None
        target = os.path.join(folder, f"{cand.key.replace(':', '-')}-{os.path.basename(path)}")
        shutil.copyfile(path, target)
    except OSError as e:
        log.warning("HI-fund: kunne ikke gemme en kopi af %s (%s)", path, e.strerror or type(e).__name__)
        return None
    log.info("HI-fund: kopi af den danske fil gemt som %s", target)
    return target


def fix_hi_after_upload(
    cfg: Config, bazarr: Bazarr, cand: Candidate, sleep, clock, warnings: list[str] | None = None
) -> bool | None:
    """Trin B8: efter synken - har Bazarr registreret vores danske tekst som HI, så ret det.

    None = ikke HI (intet gjort), True = rettet (Bazarr ser nu teksten som almindelig), False = stadig HI.
    warnings: får HI_NOTE tilføjet, hvis teksten var registreret som HI (til feedet, trin B7).
    """
    try:
        sub = danish_subtitle(bazarr, cand)
    except ApiError:
        return None
    if not is_hi(sub):
        return None
    path = str(sub["path"])
    log.warning(
        "ADVARSEL: Bazarr har registreret den danske tekst til %s som HI (%s), selv om den blev uploadet med "
        "hi=false - retter det",
        cand.title,
        path,
    )
    if warnings is not None:
        warnings.append(HI_NOTE)
    if os.path.isfile(path):
        _save_hi_find(cfg, cand, path)
    target = cand.da_path
    if hi_filename(path) and os.path.abspath(path) != os.path.abspath(target):
        if os.path.exists(target):
            log.warning("HI-mærke: %s findes allerede - %s er ikke omdøbt", target, path)
        elif not os.path.isfile(path):
            log.warning("HI-mærke: %s findes ikke på disken - kan ikke omdøbe den", path)
        else:
            try:
                os.rename(path, target)
                log.info("HI-mærke: omdøbt %s til %s", os.path.basename(path), os.path.basename(target))
            except OSError as e:
                log.warning("HI-mærke: kunne ikke omdøbe %s (%s)", path, e.strerror or type(e).__name__)
    try:
        bazarr.scan_disk(cand.media_type, cand.media_id, cand.series_id)
    except ApiError as e:
        log.warning("HI-mærke: kunne ikke bede Bazarr scanne disken for %s: %s", cand.title, e)
        return False
    deadline = clock() + HI_RECHECK
    while True:
        try:
            sub = danish_subtitle(bazarr, cand)
        except ApiError:
            sub = None
        if sub is not None and not is_hi(sub):
            log.info(
                "HI-mærke rettet: Bazarr ser nu den danske tekst til %s som almindelig (%s)", cand.title, sub["path"]
            )
            return True
        if clock() >= deadline:
            break
        sleep(POLL_SECONDS)
    shown = sub["path"] if sub else "ingen dansk tekst registreret"
    log.warning(
        "ADVARSEL: Bazarr ser stadig ikke den danske tekst til %s som almindelig efter ny scanning (%s) - tjek "
        "filen i Bazarr",
        cand.title,
        shown,
    )
    return False
