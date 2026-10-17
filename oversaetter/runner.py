"""Hovedløkken: find emner, sørg for en gyldig søgning, bed Bazarr oversætte, efterbehandl og gem status."""

from __future__ import annotations

import glob
import logging
import os
import signal
import time
from datetime import UTC, datetime, timedelta

import httpx2 as httpx

from .clients import ApiError, Bazarr, Radarr, Seerr, SeerrIndex, Sonarr, parse_date
from .config import Config
from .db import Database, iso
from .postprocess import make_client, postprocess
from .proofread import DbBudget
from .search import PAUSE_WARN, nice_name, paused_providers, search_jobs, unfinished, wait_for_jobs, waiting_text
from .selection import Candidate, Selector, Stats, choose_source
from .srt import read_file, write_file_atomic
from .translate import request_translation

log = logging.getLogger("oversaetter")

PROFILE_HINT = "Tjek at dansk er med i sprogprofilen i Bazarr."
# En søgning, der ikke er blevet færdig efter så lang tid (over flere runder), opgives og startes forfra.
STALE_SEARCH = timedelta(hours=2)


class Runner:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        http: httpx.Client | None = None,
        claude=None,
        translate=request_translation,
        sleep=time.sleep,
        clock=time.monotonic,
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
        self.sleep, self.clock = sleep, clock
        self.stopping = False

    # --- én runde -----------------------------------------------------------------
    def run_once(self) -> list[Candidate]:
        try:
            seerr_index = SeerrIndex(self.seerr.requests())
        except Exception as e:
            log.warning("Kunne ikke hente ønsker fra Seerr: %s - kun mærkede serier behandles i denne runde", e)
            seerr_index = SeerrIndex([])
        selector = Selector(self.cfg, self.bazarr, self.radarr, self.sonarr, seerr_index, self.db)
        try:
            self.sync_new_tags(selector)
        except Exception as e:
            log.warning("Kunne ikke tjekke for nye emner med mærket %s: %s", self.cfg.translate_tag, e)
        try:
            candidates, to_search, stats = selector.find()
        except ApiError as e:
            log.error("Runden er afbrudt: %s", e)
            return []
        candidates += self.search_round(selector, to_search, stats)
        self._log_round(stats)
        log.info(
            "Runde: %d mangler dansk, %d oversættes, %d søgt nu, %d venter, %d fandt dansk ved søgning, "
            "%d ikke valgt, %d allerede behandlet, %d sprunget over",
            stats.wanted,
            len(candidates),
            stats.searched,
            stats.waiting,
            stats.found_danish,
            stats.not_selected,
            stats.done_before,
            len(stats.skipped),
        )
        for cand in candidates:
            if self.stopping:
                break
            self.process(cand)
        return candidates

    # --- nye mærker: bed Bazarr synkronisere -----------------------------------------------
    def sync_new_tags(self, selector: Selector) -> None:
        """Højst én synkronisering med Radarr og én med Sonarr pr. runde, når noget nyt med mærket mangler i Bazarr."""
        tag = self.cfg.translate_tag
        needs: list[tuple[str, str, list[str]]] = []
        radarr_tag = selector.radarr_tag_id()
        if radarr_tag is not None:
            tagged = [
                m for m in selector.all_movies().values() if radarr_tag in (m.get("tags") or []) and m.get("hasFile")
            ]
            if tagged:
                known = {int(m["radarrId"]) for m in self.bazarr.movies_by_ids([int(m["id"]) for m in tagged])}
                new = [m.get("title") or f"film {m['id']}" for m in tagged if int(m["id"]) not in known]
                if new:
                    needs.append(("update_movies", "Radarr", new))
        sonarr_tag = selector.tag_id()
        if sonarr_tag is not None:
            tagged = [
                s
                for s in self.sonarr.all_series()
                if sonarr_tag in (s.get("tags") or []) and (s.get("statistics") or {}).get("episodeFileCount")
            ]
            if tagged:
                bazarr_counts = {
                    int(s["sonarrSeriesId"]): int(s.get("episodeFileCount") or 0)
                    for s in self.bazarr.series_by_ids([int(s["id"]) for s in tagged])
                }
                new = [
                    s.get("title") or f"serie {s['id']}"
                    for s in tagged
                    if int(s["statistics"]["episodeFileCount"]) > bazarr_counts.get(int(s["id"]), -1)
                ]
                if new:
                    needs.append(("update_series", "Sonarr", new))
        for task, name, titles in needs:
            shown = ", ".join(titles[:3]) + (f" og {len(titles) - 3} flere" if len(titles) > 3 else "")
            if self.cfg.dry_run:
                log.info("DRY_RUN: ville bede Bazarr synkronisere med %s nu (nyt med mærket %s: %s)", name, tag, shown)
                continue
            try:
                self.bazarr.sync(task)
                log.info("Beder Bazarr synkronisere med %s (nyt med mærket %s: %s)", name, tag, shown)
            except ApiError as e:
                log.warning("Kunne ikke bede Bazarr synkronisere med %s: %s", name, e)

    # --- gyldige søgninger ------------------------------------------------------------
    def provider_status(self, warn: bool = True):
        """Tjenester på pause lige nu (liste), eller None hvis Bazarrs tjenestestatus ikke kan læses.

        warn: skriv advarslen om tjenester, der har været på pause i over 24 timer (kun ved første tjek i runden).
        """
        try:
            paused = paused_providers(self.bazarr)
        except ApiError as e:
            log.warning("Kunne ikke læse Bazarrs tjenestestatus: %s - der søges ikke i denne runde", e)
            return None
        since = self.db.track_paused([p.name for p in paused])
        now = datetime.now(UTC)
        for p in paused if warn else []:
            if p.name in since and now - since[p.name] > PAUSE_WARN:
                log.warning(
                    "%s har været sat på pause i over %d timer - overvej at slå den fra i Bazarr",
                    nice_name(p.name),
                    PAUSE_WARN.total_seconds() // 3600,
                )
        return paused

    def _wait(self, cand: Candidate, why: str, stats: Stats) -> None:
        stats.waiting += 1
        stats.example(why.split(" til ")[0], cand.title, why)

    def _clear_search(self, cand: Candidate) -> None:
        if self.db.get(cand.key) is not None:
            self.db.upsert(cand.key, search_started_at=None, search_jobs=None)

    def _start_search(self, cand: Candidate) -> list[int] | None:
        """Bed Bazarr søge. Returnerer id'er på nye søgejob ([] hvis Bazarr søgte med det samme), None ved fejl."""
        before = search_jobs(self.bazarr.jobs())
        try:
            self.bazarr.search(
                cand.media_type,
                cand.media_id,
                series_id=cand.series_id,
                hi=cand.hi,
                timeout_s=self.cfg.search_timeout_minutes * 60,
            )
        except ApiError as e:
            log.warning("Kunne ikke bede Bazarr søge efter dansk til %s: %s", cand.title, e)
            return None
        return [i for i in search_jobs(self.bazarr.jobs()) if i not in before]

    def search_round(self, selector: Selector, items: list[Candidate], stats: Stats) -> list[Candidate]:
        """Sørg for gyldige søgninger. Returnerer de emner, der efter en gyldig søgning skal oversættes."""
        paused = self.provider_status()
        now = datetime.now(UTC)
        finished: list[Candidate] = []
        fresh: list[Candidate] = []
        for cand in items:
            row = self.db.get(cand.key)
            started = parse_date(row["search_started_at"]) if row is not None else None
            if started is None or self.cfg.dry_run:
                fresh.append(cand)
                continue
            jobs = [int(j) for j in (row["search_jobs"] or "").split(",") if j]
            left = unfinished(self.bazarr, jobs) if jobs else []
            if left is None or left:
                if now - started > STALE_SEARCH:
                    log.info("Søgningen for %s blev aldrig færdig - starter en ny", cand.title)
                    self._clear_search(cand)
                    fresh.append(cand)
                else:
                    self._wait(cand, "venter på at søgning bliver færdig", stats)
                continue
            finished.append(cand)

        if fresh and paused is None:
            for cand in fresh:
                self._wait(cand, "venter: Bazarrs tjenestestatus kunne ikke læses", stats)
        elif fresh and paused:
            why = waiting_text(paused)
            for cand in fresh:
                self._wait(cand, why, stats)
            log.info("%s - der søges ikke efter dansk til %d emner i denne runde", why[0].upper() + why[1:], len(fresh))
        elif fresh:
            started: list[tuple[Candidate, list[int]]] = []
            limit = max(0, self.cfg.max_searches_per_round)
            for cand in fresh[limit:]:
                self._wait(cand, f"venter: højst {limit} søgninger pr. runde", stats)
            for cand in fresh[:limit]:
                if self.stopping:
                    break
                if self.cfg.dry_run:
                    stats.example("søger nu", cand.title, f"ville søge nu ({cand.reason}, ingen gyldig søgning endnu)")
                    log.info(
                        "DRY_RUN: ville søge nu efter dansk til %s (%s, ingen gyldig søgning endnu)",
                        cand.title,
                        cand.reason,
                    )
                    continue
                log.info("Søger nu efter dansk til %s (%s, ingen gyldig søgning endnu)", cand.title, cand.reason)
                jobs = self._start_search(cand)
                if jobs is None:
                    stats.example("fejl", cand.title, "Bazarr kunne ikke starte søgningen")
                    continue
                stats.searched += 1
                stats.example("søger nu", cand.title, f"søger nu ({cand.reason})")
                self.db.upsert(
                    cand.key,
                    media_type=cand.media_type,
                    media_id=cand.media_id,
                    title=cand.title,
                    **({} if self.db.get(cand.key) else {"status": "soeger"}),
                    search_started_at=iso(),
                    search_jobs=",".join(str(j) for j in jobs),
                    video_path=cand.video_path,
                )
                started.append((cand, jobs))
            all_jobs = [j for _, jobs in started for j in jobs]
            if all_jobs:
                log.info("Venter på at Bazarr bliver færdig med %d søgninger", len(all_jobs))
            left = set(
                wait_for_jobs(self.bazarr, all_jobs, self.cfg.search_timeout_minutes * 60, self.sleep, self.clock)
            )
            for cand, jobs in started:
                if left & set(jobs):
                    self._wait(cand, "venter på at søgning bliver færdig", stats)
                    log.info(
                        "Søgningen for %s er ikke færdig efter %g minutter - ser på den igen i næste runde",
                        cand.title,
                        self.cfg.search_timeout_minutes,
                    )
                else:
                    finished.append(cand)

        if not finished:
            return []
        after = self.provider_status(warn=False)
        if after is None or after:
            why = waiting_text(after) if after else "venter: Bazarrs tjenestestatus kunne ikke læses"
            for cand in finished:
                self._clear_search(cand)
                self._wait(cand, why + " (søgningen tæller ikke)", stats)
                log.info("Søgningen for %s tæller ikke: %s - prøver igen senere", cand.title, why)
            return []
        ready: list[Candidate] = []
        for cand in finished:
            self.db.upsert(cand.key, search_at=iso(), search_started_at=None, search_jobs=None)
            if self.db.get(cand.key)["status"] == "soeger":
                self.db.upsert(cand.key, status="soegt")
            try:
                item = self.bazarr.item(cand.media_type, cand.media_id)
            except ApiError as e:
                log.warning("Kunne ikke spørge Bazarr om %s efter søgningen: %s", cand.title, e)
                continue
            out = selector.after_search(cand, item, stats)
            if out:
                ready.append(out)
        return ready

    def _log_round(self, stats: Stats) -> None:
        """Forklar runden: Bazarrs rå tal, eksempler på valg/fravalg og fejl."""
        if stats.bazarr_movies == 0 and stats.bazarr_episodes == 0:
            log.info("Bazarr melder ingen film eller afsnit, der mangler dansk. %s", PROFILE_HINT)
            return
        log.info(
            "Bazarr melder %d film og %d afsnit på wanted-listen (heraf %d film og %d afsnit uden dansk)",
            stats.bazarr_movies,
            stats.bazarr_episodes,
            stats.wanted_movies,
            stats.wanted_episodes,
        )
        if stats.wanted == 0:
            log.info("Ingen af dem mangler dansk ifølge Bazarr. %s", PROFILE_HINT)
        examples = stats.pick_examples()
        for text in examples:
            log.info("Eksempel: %s", text)
        if len(stats.examples) > len(examples):
            log.info("... og %d flere (se DEBUG-loggen)", len(stats.examples) - len(examples))
            for _, text in stats.examples:
                log.debug("Valg: %s", text)
        for msg, count in stats.errors.items():
            if count > 1:
                log.warning("Samme fejl ramte %d emner: %s", count, msg)

    def process(self, cand: Candidate) -> None:
        if self.cfg.dry_run:
            log.info(
                "DRY_RUN: ville oversætte nu: %s (%s, gyldig søgning fandt ikke dansk) fra %s til %s",
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
            "Gyldig søgning fandt ikke dansk - beder Bazarr oversætte %s (%s, forsøg %d) fra %s",
            cand.title,
            cand.reason,
            attempts,
            cand.source_desc,
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
        self.log_start()
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

    def log_start(self) -> None:
        log.info(
            "Oversætter startet. DRY_RUN=%s, runde hvert %g minut, budget %.2f USD/måned",
            self.cfg.dry_run,
            self.cfg.poll_minutes,
            self.cfg.monthly_budget_usd,
        )
        for key in self.cfg.ignored_keys:
            log.info("%s i .env bruges ikke længere og ignoreres (se README under Indstillinger og beslutninger)", key)
        if self.cfg.dry_run:
            log.info("DRY_RUN er slået til: der søges, synkroniseres og oversættes ikke, og der skrives ingen filer")
        if self.claude is None:
            log.warning("Ingen Anthropic-nøgle fundet i %s - korrektur springes over", self.cfg.anthropic_api_key_file)

    def _stop(self, *_):
        log.info("Stopper efter den igangværende opgave")
        self.stopping = True


def main(cfg: Config) -> None:
    db = Database(os.path.join(cfg.data_dir, "oversaetter.db"))
    Runner(cfg, db, claude=make_client(cfg)).loop()
