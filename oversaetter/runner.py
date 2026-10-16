"""Hovedløkken: find kandidater, bed Bazarr oversætte, efterbehandl og gem status."""

from __future__ import annotations

import glob
import logging
import os
import signal
import time

import httpx2 as httpx

from .clients import Bazarr, Radarr, Seerr, SeerrIndex, Sonarr, parse_date
from .config import Config
from .db import Database, iso
from .postprocess import make_client, postprocess
from .proofread import DbBudget
from .selection import Candidate, Selector, choose_source
from .srt import read_file, write_file_atomic
from .translate import request_translation

log = logging.getLogger("oversaetter")


class Runner:
    def __init__(
        self, cfg: Config, db: Database, http: httpx.Client | None = None, claude=None, translate=request_translation
    ):
        self.cfg = cfg
        self.db = db
        self.http = http or httpx.Client()
        self.bazarr = Bazarr(cfg, self.http)
        self.radarr = Radarr(cfg, self.http)
        self.sonarr = Sonarr(cfg, self.http)
        self.seerr = Seerr(cfg, self.http)
        self.claude = claude
        self.translate = translate
        self.stopping = False

    # --- én runde -----------------------------------------------------------------
    def run_once(self) -> list[Candidate]:
        try:
            seerr_index = SeerrIndex(self.seerr.requests())
        except Exception as e:
            log.warning("Kunne ikke hente ønsker fra Seerr (%s) - kun mærkede serier behandles i denne runde", e)
            seerr_index = SeerrIndex([])
        selector = Selector(self.cfg, self.bazarr, self.radarr, self.sonarr, seerr_index, self.db)
        candidates, stats = selector.find()
        log.info(
            "Runde: %d mangler dansk, %d kandidater, %d venter på forsinkelse, %d ikke valgt, "
            "%d allerede behandlet, %d sprunget over",
            stats.wanted,
            len(candidates),
            stats.waiting,
            stats.not_selected,
            stats.done_before,
            len(stats.skipped),
        )
        for cand in candidates:
            if self.stopping:
                break
            self.process(cand)
        return candidates

    def process(self, cand: Candidate) -> None:
        if self.cfg.dry_run:
            log.info(
                "DRY_RUN: ville bede Bazarr oversætte %s (%s) fra %s til %s",
                cand.title,
                cand.reason,
                cand.source_desc,
                cand.da_path,
            )
            return
        row = self.db.get(cand.key)
        attempts = (row["attempts"] if row else 0) + 1
        source = cand.source_path or f"subtitles_id:{cand.subtitles_id}"
        self.db.upsert(
            cand.key,
            media_type=cand.media_type,
            media_id=cand.media_id,
            title=cand.title,
            status="i_gang",
            attempts=attempts,
            video_path=cand.video_path,
            source=source,
            da_path=cand.da_path,
            started_at=iso(),
            reason=cand.reason,
        )
        log.info(
            "Beder Bazarr oversætte %s (%s, forsøg %d) fra %s", cand.title, cand.reason, attempts, cand.source_desc
        )
        outcome = self.translate(self.bazarr, cand, self.cfg.translate_timeout_minutes)
        if not outcome.ok:
            self._failed(cand.key, cand.title, attempts, outcome.reason, outcome.permanent)
            return
        log.info("Bazarr har skrevet %s", cand.da_path)
        self.finish(
            cand.key, cand.title, cand.media_type, cand.media_id, cand.video_path, cand.da_path, cand.source_path
        )

    def _failed(self, key: str, title: str, attempts: int, reason: str, permanent: bool = False) -> None:
        if permanent:
            status = "ikke_muligt"
            log.warning("Springer over: %s - %s", title, reason)
        elif attempts >= 2:
            status = "fejlet"
            log.error("Fejlet: %s - %s. Prøves ikke igen automatisk", title, reason)
        else:
            status = "forsoeg_igen"
            log.warning("Oversættelse mislykkedes for %s - %s. Prøves igen i næste runde", title, reason)
        self.db.upsert(key, status=status, reason=reason)

    # --- efterbehandling ------------------------------------------------------------
    def _english_path(self, media_type: str, media_id: int, video_path: str, source_path: str | None) -> str | None:
        if source_path and os.path.isfile(source_path):
            return source_path
        # Indlejret kilde: Bazarr har udtrukket sporet til en ekstern fil.
        try:
            item = self.bazarr.item(media_type, media_id) or {}
            path, _, _ = choose_source(item.get("subtitles") or [])
            if path:
                return path
        except Exception:
            pass
        stem = glob.escape(os.path.splitext(video_path)[0])
        found = sorted(p for p in glob.glob(stem + ".en*.srt") if ".forced" not in p.lower())
        return found[0] if found else None

    def finish(self, key, title, media_type, media_id, video_path, da_path, source_path) -> None:
        try:
            da = read_file(da_path)
            en_path = self._english_path(media_type, media_id, video_path, source_path)
            en = read_file(en_path) if en_path else []
            if not en:
                log.warning("Fandt ingen engelsk fil til sammenligning for %s - kontrollen bliver begrænset", title)
            result = postprocess(da, en, self.cfg, self.claude, DbBudget(self.db), key)
            write_file_atomic(da_path, result.cues)
        except Exception as e:
            log.error("Efterbehandling af %s fejlede (%s) - Bazarrs danske fil er ladt urørt", title, e)
            self.db.upsert(key, status="faerdig", reason=f"efterbehandling fejlede: {e}")
            return
        self.db.upsert(
            key,
            status="faerdig",
            reason=result.proofread,
            cost_usd=result.cost,
            flags_before=result.flags_before,
            flags_after=result.flags_after,
            fixes=result.fixes,
        )
        log.info("Færdig: %s - %s", title, result.summary())

    def resume(self) -> None:
        """Emner der stod 'i_gang' da programmet stoppede."""
        if self.cfg.dry_run:
            return
        for row in self.db.with_status("i_gang"):
            started = parse_date(row["started_at"])
            da_path = row["da_path"] or ""
            mtime = os.path.getmtime(da_path) if da_path and os.path.exists(da_path) else None
            if mtime and started and mtime > started.timestamp():
                log.info("Genoptager efterbehandling af %s", row["title"])
                source = row["source"] if row["source"] and not row["source"].startswith("subtitles_id:") else None
                self.finish(
                    row["key"], row["title"], row["media_type"], row["media_id"], row["video_path"], da_path, source
                )
            else:
                self._failed(row["key"], row["title"], row["attempts"], "programmet blev stoppet undervejs")

    # --- løkken ---------------------------------------------------------------------
    def loop(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        log.info(
            "Oversætter startet. DRY_RUN=%s, runde hvert %g minut, budget %.2f USD/måned",
            self.cfg.dry_run,
            self.cfg.poll_minutes,
            self.cfg.monthly_budget_usd,
        )
        if self.cfg.dry_run:
            log.info("DRY_RUN er slået til: der kaldes ikke translate og der skrives ingen filer")
        if self.claude is None:
            log.warning("Ingen Anthropic-nøgle fundet i %s - korrektur springes over", self.cfg.anthropic_api_key_file)
        self.resume()
        while not self.stopping:
            try:
                self.run_once()
            except Exception as e:
                log.error("Runden fejlede: %s", e)
            deadline = time.monotonic() + self.cfg.poll_minutes * 60
            while not self.stopping and time.monotonic() < deadline:
                time.sleep(1)
        log.info("Oversætter stoppet")

    def _stop(self, *_):
        log.info("Stopper efter den igangværende opgave")
        self.stopping = True


def main(cfg: Config) -> None:
    db = Database(os.path.join(cfg.data_dir, "oversaetter.db"))
    Runner(cfg, db, claude=make_client(cfg)).loop()
