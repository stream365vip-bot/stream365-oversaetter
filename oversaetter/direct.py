"""TRANSLATE_MODE=direct: programmet oversætter selv med Claude i bidder og afleverer resultatet til Bazarr.

Besluttet 18.10.2026 efter modeltesten på Do Not Enter (se README): Opus 5.5 direkte, ingen korrektur af hele
filen - kun en lille rettelsesrunde for de replikker, den gratis kontrol flager.

Flow for ét emne: hent den engelske tekst (ekstern .srt eller indlejret tekstspor via ffmpeg), oversæt med
oversaet.translate_all, ret de flagede replikker i ét ekstra kald, sæt kreditlinjen ind, gem filen og upload den
til Bazarr. Fejler uploaden, skrives filen ved siden af videoen, og Bazarr bedes om at scanne disken.

BATCH_API=true (trin B6): ordlisten og bid 1 direkte, bid 2 og frem som én Anthropic Message Batch til halv pris
(oversaet.BatchApi). Batchens id gemmes på titlen, til svarene er hentet, så en genstart henter dem i stedet for
at sende igen.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace

from . import hi
from .checks import FLAG_TEXT, EnglishIndex, check_all, count_info, fix_flags, plain
from .clients import ApiError, Bazarr
from .config import BATCH_FACTOR, Config, model_price
from .credit import apply_credit
from .deadline import call_with_timeout
from .konto import account_problem
from .ordliste import Wordlist, merge_previous
from .oversaet import (
    BatchApi,
    BatchStats,
    build_system,
    drop_empty,
    filler_positions,
    make_glossary,
    speaker_notes,
    translate_all,
)
from .proofread import OUTPUT_SCHEMA as FIX_SCHEMA
from .proofread import Budget, validate_fix
from .selection import Candidate, has_danish
from .srt import Cue, decode, parse, read_file, write_file_atomic

log = logging.getLogger("oversaetter")

CALL_TIMEOUT = 600.0  # højst så mange sekunder på ét svar fra modellen
FFMPEG_TIMEOUT = 900.0  # højst så mange sekunder på ffprobe/ffmpeg (store filer over netværksdrev)
UPLOAD_TIMEOUT = 120.0
UPLOAD_WAIT = 180.0  # så længe ventes der på, at Bazarr har gemt den uploadede fil
POLL_SECONDS = 5
FIX_CONTEXT = 2
FIX_MAX_TOKENS = 16000
BATCH = 50
BATCH_LOG_EVERY = 120  # BATCH_API: en INFO-linje med batchens status så ofte (sekunder)
MIN_TRANSLATED = 0.95  # trin B8: en titel uploades kun, hvis mindst så stor en andel af replikkerne er oversat
UPLOADED = "uploadet til Bazarr"  # deliver() efter en vellykket upload (så beder runneren Plex scanne, trin B4g)

# Tekstbaserede undertekstformater, som ffmpeg kan lave om til srt. Alt andet (PGS, VobSub, DVB ...) springes over.
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
ENGLISH = {"eng", "en", "english"}
SDH_RE = re.compile(r"\b(sdh|cc|hi|hoh|hearing)\b", re.IGNORECASE)

FIX_PROMPT = """You are a senior editor of Danish subtitles. An automatic check flagged some lines of a Danish \
translation from English. Fix ONLY the lines with "fix": true (each has "en", "da" and "flag"). Lines with \
"context": true are neighbours for understanding only: never return them. Write natural spoken Danish with the \
same meaning, at most 2 lines of at most {max_chars} characters, keep tags such as <i> and write a line break as \
\\n. Names stay unchanged. "da" must contain only the finished Danish subtitle: no notes, no explanations. If a \
flagged line is already correct, return it unchanged. Reply ONLY with JSON {{"fixes":[{{"i":<number>,"da":"<Danish \
text>"}}]}}."""


class NoEnglish(Exception):
    """Ingen engelsk tekst, der kan oversættes. permanent=True: prøv ikke igen (fx kun billedbaserede spor)."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class DirectOutcome:
    ok: bool
    reason: str = ""
    permanent: bool = False
    lines: int = 0
    flags_before: int = 0  # rettelsesflag (uden informationsflag)
    flags_after: int = 0
    info_flags: int = 0  # replikker med informationsflag (fx reading_speed) - tælles, men rettes ikke
    fixes: int = 0
    fallback_english: int = 0
    by_fallback: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    delivery: str = ""
    credit: str = ""
    model: str = ""
    switches: str = ""  # kontakterne fra 18.10.2026, kun når de ikke står på standard
    removed_empty: int = 0  # replikker med kun fyldlyde (v4), fjernet fra filen
    en_hi: bool = False  # den engelske kilde var HI/SDH (navn/disposition eller indhold)
    glossary_terms: int | None = None  # None = ingen ordliste
    glossary: Wordlist | None = None
    batch: BatchStats | None = None  # BATCH_API=true
    batch_cost: float = 0.0  # den del af cost, der er batchens (med BATCH_FACTOR)

    def summary(self) -> str:
        extra = ""
        if self.switches:
            extra += f", {self.switches}"
            if self.glossary_terms is not None:
                extra += f", ordliste: {self.glossary_terms} termer"
            extra += f", tomme fjernet: {self.removed_empty}"
        price = f"pris: {self.cost:.4f} USD"
        b = self.batch
        if b is not None:
            price += (
                f" (batch {self.batch_cost:.4f} USD, direkte {self.cost - self.batch_cost:.4f} USD), "
                f"batch: {b.sent} bidder{' fra gemt batch' if b.reused else ''}, {b.ok} lykkedes, "
                f"{b.direct_after} direkte, ventetid {duration(b.seconds)}"
            )
        return (
            f"model: {self.model}, replikker: {self.lines}, flag: {self.flags_before}, "
            f"informationsflag: {self.info_flags}, rettelser: {self.fixes}, "
            f"flag efter: {self.flags_after}, beholdt engelsk: {self.fallback_english}, "
            f"fallback-model: {self.by_fallback}, {price}, tid: {duration(self.seconds)}, "
            f"{self.delivery}, {self.credit}{extra}"
        )


def duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s} s" if m else f"{s} s"


# --- engelsk kilde -------------------------------------------------------------------------------------
def _run(run_cmd, args: list[str], timeout: float):
    try:
        return run_cmd(args, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise NoEnglish(f"{args[0]} findes ikke i containeren - kan ikke læse indlejrede spor") from None
    except subprocess.TimeoutExpired:
        raise NoEnglish(f"{args[0]} blev ikke færdig inden for {timeout:g} sekunder") from None


def _sdh(stream: dict) -> bool:
    tags = stream.get("tags") or {}
    disp = stream.get("disposition") or {}
    return bool(disp.get("hearing_impaired")) or bool(SDH_RE.search(str(tags.get("title") or "")))


def pick_stream(streams: list[dict]) -> tuple[dict | None, str]:
    """Vælg det engelske tekstspor (ikke forced, helst SDH/HI). Returnerer (spor, hvorfor intet blev valgt)."""
    english = [
        s
        for s in streams
        if str((s.get("tags") or {}).get("language") or "").lower() in ENGLISH
        and not (s.get("disposition") or {}).get("forced")
        and "forced" not in str((s.get("tags") or {}).get("title") or "").lower()
    ]
    if not english:
        return None, "ingen engelsk tekstkilde i videoen"
    text = [s for s in english if str(s.get("codec_name") or "").lower() in TEXT_CODECS]
    if not text:
        return None, "kun PGS/VobSub (billedbaseret engelsk tekst) - kan ikke oversættes"
    text.sort(key=lambda s: not _sdh(s))  # sorted er stabil: første SDH/HI-spor vinder (talernavne hjælper)
    return text[0], ""


def embedded_english(
    video: str, run_cmd=subprocess.run, timeout: float = FFMPEG_TIMEOUT
) -> tuple[list[Cue], str, dict]:
    """Træk det engelske tekstspor ud af videoen med ffmpeg. Returnerer (replikker, beskrivelse, ffprobes spor).

    Rejser NoEnglish, hvis der ikke er et brugbart.
    """
    probe = _run(
        run_cmd,
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired",
            "-of",
            "json",
            video,
        ],
        timeout,
    )
    if probe.returncode != 0:
        why = decode(probe.stderr or b"").strip()[:200]
        raise NoEnglish(f"ffprobe kunne ikke læse {os.path.basename(video)} ({why or 'ukendt fejl'})")
    try:
        streams = json.loads(decode(probe.stdout or b"{}")).get("streams") or []
    except ValueError:
        raise NoEnglish("ffprobe gav et ugyldigt svar") from None
    stream, why = pick_stream(streams)
    if stream is None:
        raise NoEnglish(why, permanent=True)
    index = int(stream["index"])
    out = _run(
        run_cmd,
        ["ffmpeg", "-nostdin", "-v", "error", "-i", video, "-map", f"0:{index}", "-f", "srt", "-"],
        timeout,
    )
    if out.returncode != 0:
        why = decode(out.stderr or b"").strip()[:200]
        raise NoEnglish(f"ffmpeg kunne ikke trække spor {index} ud ({why or 'ukendt fejl'})")
    cues = parse(decode(out.stdout or b""))
    title = (stream.get("tags") or {}).get("title")
    desc = f"indlejret engelsk spor {index} ({stream.get('codec_name')}{', ' + title if title else ''}, via ffmpeg)"
    return cues, desc, stream


def english_source(cand: Candidate, run_cmd=subprocess.run) -> tuple[list[Cue], str, str, bool]:
    """Som english_cues, plus sporets titel/filnavnet og om navn/disposition siger HI/SDH."""
    if cand.source_path:
        try:
            cues = read_file(cand.source_path)
        except OSError as e:
            raise NoEnglish(f"kan ikke læse {cand.source_path} ({e.strerror or type(e).__name__})") from None
        desc = cand.source_desc or f"ekstern engelsk: {os.path.basename(cand.source_path)}"
        label = os.path.basename(cand.source_path)
        by_name = cand.source_hi or hi.hi_filename(cand.source_path)
    else:
        cues, desc, stream = embedded_english(cand.video_path, run_cmd)
        label = str((stream.get("tags") or {}).get("title") or f"spor {stream['index']}")
        by_name = _sdh(stream)
    cues = [c for c in cues if plain(c.text).strip()]
    if not cues:
        raise NoEnglish(f"der er ingen replikker i den engelske tekst ({desc})")
    return cues, desc, label, by_name


def english_cues(cand: Candidate, run_cmd=subprocess.run) -> tuple[list[Cue], str]:
    """Den engelske tekst for emnet (uden tomme replikker) og en beskrivelse til loggen."""
    cues, desc, _, _ = english_source(cand, run_cmd)
    return cues, desc


# --- rettelsesrunden -----------------------------------------------------------------------------------
def fix_flagged(
    da: list[Cue],
    en: list[str],
    flags: dict[int, list[str]],
    cfg: Config,
    client,
    timeout: float,
    on_usage,
    extra: str = "",
) -> dict[int, str]:
    """Ét kald til TRANSLATE_MODEL med de flagede replikker og 2 nabolinjer på hver side. Returnerer rettelser.

    En tom rettelse ("") betyder: slet replikken (kun når den engelske tekst er ren lydbeskrivelse/musik, trin B4f).

    extra: filmkontekst og ordliste (CONTEXT/GLOSSARY=on), lægges efter instruksen.
    """
    chosen = sorted(flags)[: max(0, cfg.max_fix_lines)]
    if not chosen:
        return {}
    if len(flags) > len(chosen):
        log.info("Rettelsesrunde: %d flagede replikker - kun de første %d sendes", len(flags), len(chosen))
    show: set[int] = set()
    for pos in chosen:
        show.update(range(max(0, pos - FIX_CONTEXT), min(len(da), pos + FIX_CONTEXT + 1)))
    lines = []
    for pos in sorted(show):
        item = {"i": pos + 1, "en": en[pos], "da": da[pos].text}
        if pos in chosen:
            item.update(fix=True, flag=[FLAG_TEXT.get(f, f) for f in flags[pos]])
        else:
            item["context"] = True
        lines.append(item)
    user = 'Fix only the lines with "fix": true and reply with JSON.\n\n' + json.dumps(
        {"lines": lines}, ensure_ascii=False
    )
    try:
        response = call_with_timeout(
            client.messages.create,
            timeout,
            model=cfg.translate_model,
            max_tokens=FIX_MAX_TOKENS,
            system=FIX_PROMPT.format(max_chars=min(40, cfg.max_line_chars)) + (f"\n\n{extra}" if extra else ""),
            messages=[{"role": "user", "content": user}],
            output_config={"effort": cfg.translate_effort, "format": {"type": "json_schema", "schema": FIX_SCHEMA}},
        )
    except TimeoutError:
        log.error("Rettelsesrunden fik intet svar inden for %g sekunder (timeout) - ingen rettelser", timeout)
        return {}
    except Exception as e:  # netværk, API-fejl osv.
        blocked = account_problem(e)
        if blocked is not None:  # trin B8: udgiftsgrænse/nøglefejl - hele titlen stoppes, intet uploades
            raise blocked from e
        log.error("Rettelsesrunden fejlede (%s: %s) - ingen rettelser", type(e).__name__, str(e)[:200])
        return {}
    usage = getattr(response, "usage", None)

    def get(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    on_usage(
        cfg.translate_model,
        get("input_tokens"),
        get("output_tokens"),
        get("cache_read_input_tokens"),
        get("cache_creation_input_tokens"),
    )
    stop = getattr(response, "stop_reason", None)
    if stop not in ("end_turn", None):
        log.error("Rettelsesrunden blev afbrudt (stop_reason=%s) - ingen rettelser", stop)
        return {}
    text = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), "")
    try:
        fixes = json.loads(text)["fixes"]
        if not isinstance(fixes, list):
            raise ValueError("fixes er ikke en liste")
    except (ValueError, KeyError, TypeError) as e:
        log.error("Ugyldigt svar i rettelsesrunden (%s) - ingen rettelser", e)
        return {}
    allowed = {pos + 1 for pos in chosen}
    en_by_i = {pos + 1: en[pos] for pos in chosen}
    out: dict[int, str] = {}
    for fix in fixes:
        if (
            isinstance(fix, dict)
            and fix.get("i") in allowed
            and isinstance(fix.get("da"), str)
            and not plain(fix["da"]).strip()
            and hi.only_markup(en_by_i[fix["i"]])
        ):
            out[fix["i"] - 1] = ""  # kun lydbeskrivelse/musik på engelsk: tom rettelse = slet replikken (trin B4f)
            continue
        ok, why = validate_fix(fix, allowed, en_by_i)
        if not ok:
            shown = fix.get("da") if isinstance(fix, dict) else fix
            log.warning("Rettelse kasseret: %s: %r", why, str(shown)[:120])
            continue
        pos = fix["i"] - 1
        new = fix["da"].replace("\r\n", "\n").replace("\\n", "\n").strip()
        if new != da[pos].text:
            out[pos] = new
    return out


# --- ét emne -------------------------------------------------------------------------------------------
class DbBatchIds:
    """BATCH_API: batchens id på titlen (items.batch_id), fra den er sendt, til svarene er hentet."""

    def __init__(self, db, on_save=None):
        self.db = db
        self.on_save = on_save  # trin B11: kaldes med id'et (eller None), så status-jobbet kan se batch-ventetiden

    def saved(self, key: str) -> str | None:
        row = self.db.get(key)
        return row["batch_id"] if row is not None else None

    def save(self, key: str, batch_id: str | None) -> None:
        self.db.upsert(key, batch_id=batch_id)
        if self.on_save is not None:
            self.on_save(batch_id)


class DirectTranslator:
    def __init__(
        self,
        cfg: Config,
        bazarr: Bazarr,
        client,
        budget: Budget,
        run_cmd=subprocess.run,
        sleep=time.sleep,
        clock=time.monotonic,
        context_for=None,
        glossaries_for=None,
        batch_ids: DbBatchIds | None = None,
    ):
        """context_for(cand) -> kontekstblok eller None; glossaries_for(cand) -> tidligere ordlister (JSON) i serien.

        batch_ids: hvor batchens id gemmes (BATCH_API=true); None = intet gemmes.
        """
        self.cfg, self.bazarr, self.client, self.budget = cfg, bazarr, client, budget
        self.run_cmd, self.sleep, self.clock = run_cmd, sleep, clock
        self.context_for, self.glossaries_for = context_for, glossaries_for
        self.batch_ids = batch_ids

    def batch_api(self, cand: Candidate) -> BatchApi | None:
        """BATCH_API=true: Message Batch for bid 2 og frem; en gemt batch (fra før en genstart) hentes i stedet."""
        if not self.cfg.batch_api:
            return None
        saved = self.batch_ids.saved(cand.key) if self.batch_ids is not None else None
        if saved:
            log.info("%s har en gemt batch (%s) fra før - svarene hentes derfra", cand.title, saved)

        def on_id(batch_id: str | None) -> None:
            if self.batch_ids is not None:
                self.batch_ids.save(cand.key, batch_id)

        return BatchApi(
            self.client,
            self.cfg.batch_timeout_minutes,
            sleep=self.sleep,
            clock=self.clock,
            log_every=BATCH_LOG_EVERY,
            batch_id=saved,
            on_id=on_id,
            quiet=True,
            resend=True,
        )

    def work_path(self, cand: Candidate) -> str:
        return os.path.join(self.cfg.data_dir, "direkte", cand.key.replace(":", "-") + ".da.srt")

    def translate(
        self, cand: Candidate, en_cues: list[Cue], desc: str, label: str = "", by_name: bool = False
    ) -> DirectOutcome:
        """label/by_name: sporets titel eller filnavnet, og om navn/disposition siger HI (fra english_source)."""
        cfg = self.cfg
        started = self.clock()
        deadline = started + cfg.translate_timeout_minutes * 60
        spent = {"cost": 0.0}

        def on_usage(model: str, inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> None:
            cost = model_price(model, inp, out, cache_read, cache_write)
            if cost is None:
                cost = cfg.price(inp, out, cache_read, cache_write)
                log.warning("Ukendt pris for %s - bruger PRICE_INPUT_PER_M/PRICE_OUTPUT_PER_M", model)
            spent["cost"] += cost
            self.budget.record(cand.key, inp + cache_read + cache_write, out, cost)

        # trin B4: lydbeskrivelser, musiklinjer og talernavne ud før bidderne
        hi_result = hi.clean(en_cues, drop_lyrics=cfg.drop_lyrics)
        en_hi, kind = hi.source_kind(hi_result.stats, by_name, label)
        log.info("%s", kind)
        en_cues = hi_result.cues
        if not en_cues:
            return DirectOutcome(
                ok=False,
                model=cfg.translate_model,
                reason=f"ingen replikker tilbage efter HI-rensningen ({desc})",
                en_hi=en_hi,
            )
        log.info(
            "Gyldig søgning fandt ikke dansk - oversætter %s selv (%s) med %s (effort %s): %d replikker fra %s",
            cand.title,
            cand.reason,
            cfg.translate_model,
            cfg.translate_effort,
            len(en_cues),
            desc,
        )
        en = [c.text for c in en_cues]
        outcome = DirectOutcome(ok=False, model=cfg.translate_model, lines=len(en_cues), en_hi=en_hi)
        if cfg.switches() != Config().switches():
            outcome.switches = cfg.switches()
            log.info("Kontakter for %s: %s", cand.title, outcome.switches)
        context = self.context_for(cand) if cfg.context and self.context_for is not None else None
        wordlist = None
        if cfg.glossary:
            previous = merge_previous(self.glossaries_for(cand)) if self.glossaries_for is not None else None
            if previous is not None and previous.terms:
                log.info("Ordliste: genbruger %d termer fra tidligere afsnit af serien", len(previous.terms))
            wordlist = make_glossary(
                "anthropic",
                self.client,
                cfg.translate_model,
                cfg.translate_effort,
                en,
                context,
                previous,
                max(1.0, min(CALL_TIMEOUT, deadline - self.clock())),
                on_usage,
                hi_result.speakers,
                hi_result.notes,
            )
            outcome.glossary = wordlist
            outcome.glossary_terms = len(wordlist.terms) if wordlist is not None else None
        system = build_system(cfg.prompt_version, "anthropic", context, wordlist)
        batch_api = self.batch_api(cand)
        res = translate_all(
            en,
            "anthropic",
            self.client,
            cfg.translate_model,
            cfg.translate_effort,
            BATCH,
            CALL_TIMEOUT,
            fallback_model=cfg.fallback_model,
            on_usage=on_usage,
            deadline=deadline,
            clock=self.clock,
            system=system,
            parallel=cfg.parallel,
            empty_ok=filler_positions(en, cfg.prompt_version) | hi.markup_positions(en),  # trin B4f
            cache=cfg.prompt_cache,
            notes=speaker_notes(hi_result, cfg.prompt_version),
            batch_api=batch_api,
        )
        if batch_api is not None and self.batch_ids is not None:
            self.batch_ids.save(cand.key, None)  # svarene er hentet (eller opgivet)
        if res.batch is not None:
            outcome.batch = res.batch
            outcome.batch_cost = self.batch_usage(cand, res.batch)
            spent["cost"] += outcome.batch_cost
            deadline += res.batch.seconds  # ventetiden på batchen tæller ikke med i TRANSLATE_TIMEOUT_MINUTES
        if res.cache_read or res.cache_write:
            log.info("Prompt-cache: %d tokens læst, %d tokens skrevet", res.cache_read, res.cache_write)
        outcome.fallback_english, outcome.by_fallback = len(res.fallback), res.by_fallback
        if res.timed_out:
            outcome.cost, outcome.seconds = spent["cost"], self.clock() - started
            outcome.reason = f"tidsgrænsen på {cfg.translate_timeout_minutes:g} minutter blev nået under oversættelsen"
            return outcome
        if len(res.fallback) == len(en_cues):
            outcome.cost, outcome.seconds = spent["cost"], self.clock() - started
            outcome.reason = "ingen replikker blev oversat (se fejlene ovenfor)"
            return outcome
        translated = len(en_cues) - len(res.fallback)
        if translated < MIN_TRANSLATED * len(en_cues):
            # Trin B8: "beholder engelsk tekst" er kun i orden for enkelte replikker, ikke for hele bidder.
            outcome.cost, outcome.seconds = spent["cost"], self.clock() - started
            outcome.reason = (
                f"kun {translated} af {len(en_cues)} replikker ({100 * translated / len(en_cues):.0f} %) blev "
                f"oversat - under {100 * MIN_TRANSLATED:.0f} %, intet uploadet"
            )
            log.error("%s: %s (se fejlene ovenfor)", cand.title, outcome.reason)
            return outcome
        if res.fallback:
            log.error(
                "ADVARSEL: %d af %d replikker i %s beholder den engelske tekst (se ovenfor)",
                len(res.fallback),
                len(en_cues),
                cand.title,
            )

        kept, fallback = drop_empty(res.da, res.fallback)
        outcome.removed_empty = len(res.da) - len(kept)
        if outcome.removed_empty:
            log.info("%d replikker med kun fyldlyde er fjernet fra filen", outcome.removed_empty)
        da = [replace(en_cues[p], text=res.da[p]) for p in kept]
        en = [en_cues[p].text for p in kept]
        en_index = EnglishIndex(en_cues)
        bad_words = cfg.bad_words()
        kept_english = set(fallback)
        strict = cfg.prompt_version == "v4"
        speed = cfg.reading_speed_max
        flags = check_all(da, en_index, cfg.max_line_chars, bad_words, wordlist, strict, speed)
        flags = {p: f for p, f in flags.items() if p not in kept_english}  # afviste replikker sendes ikke igen
        outcome.info_flags = count_info(flags)
        flags = fix_flags(flags)  # informationsflag (reading_speed) sendes aldrig i rettelsesrunden
        outcome.flags_before = len(flags)
        log.info(
            "Kontrol efter oversættelse: %d replikker flaget til rettelse, %d med informationsflag",
            len(flags),
            outcome.info_flags,
        )
        if flags:
            left = deadline - self.clock()
            if self.budget.spent() >= cfg.monthly_budget_usd:
                log.warning("Månedens budget på %.2f USD er nået - ingen rettelsesrunde", cfg.monthly_budget_usd)
            elif left <= 1:
                log.warning("Tidsgrænsen er nået - rettelsesrunden springes over")
            else:
                extra = "\n\n".join(x for x in (context, wordlist.block() if wordlist else "") if x)
                fixes = fix_flagged(da, en, flags, cfg, self.client, min(CALL_TIMEOUT, left), on_usage, extra)
                da = [replace(c, text=fixes.get(p, c.text)) for p, c in enumerate(da)]
                outcome.fixes = len(fixes)
                sent = min(len(flags), cfg.max_fix_lines)
                log.info("Rettelsesrunde: %d af %d flagede replikker rettet", len(fixes), sent)
                deleted = {p for p, t in fixes.items() if not t}
                if deleted:  # trin B4f: kun lydbeskrivelse/musik på engelsk og tom rettelse - replikken slettes
                    keep = [p for p in range(len(da)) if p not in deleted]
                    new = {p: i for i, p in enumerate(keep)}
                    da, en = [da[p] for p in keep], [en[p] for p in keep]
                    kept_english = {new[p] for p in kept_english if p in new}
                    outcome.removed_empty += len(deleted)
                    log.info("Rettelsesrunde: %d replikker med kun lydbeskrivelse/musik er slettet", len(deleted))
        after = fix_flags(check_all(da, en_index, cfg.max_line_chars, bad_words, wordlist, strict, speed))
        outcome.flags_after = len([p for p in after if p not in kept_english])

        final, outcome.credit = apply_credit(da, cfg.credit_text)
        work = self.work_path(cand)
        os.makedirs(os.path.dirname(work), exist_ok=True)
        write_file_atomic(work, final)
        outcome.delivery = self.deliver(cand, work, final)
        outcome.ok = True
        outcome.cost, outcome.seconds = spent["cost"], self.clock() - started
        return outcome

    def batch_usage(self, cand: Candidate, b: BatchStats) -> float:
        """Batchens tokens til halv pris (BATCH_FACTOR) i budgettet. Returnerer prisen."""
        tokens = (b.input_tokens, b.output_tokens, b.cache_read, b.cache_write)
        if not any(tokens):
            return 0.0
        model = self.cfg.translate_model
        cost = model_price(model, *tokens, factor=BATCH_FACTOR)
        if cost is None:
            cost = BATCH_FACTOR * self.cfg.price(*tokens)
            log.warning("Ukendt pris for %s - bruger PRICE_INPUT_PER_M/PRICE_OUTPUT_PER_M", model)
        self.budget.record(cand.key, b.input_tokens + b.cache_read + b.cache_write, b.output_tokens, cost)
        return cost

    # --- aflevering til Bazarr ---------------------------------------------------------------------------
    def _bazarr_has_danish(self, cand: Candidate) -> bool:
        try:
            item = self.bazarr.item(cand.media_type, cand.media_id) or {}
        except ApiError:
            return False
        return has_danish(item.get("subtitles") or []) is not None

    def _wait_for_bazarr(self, cand: Candidate, before: float | None) -> bool:
        """Bazarrs upload kører som et job i baggrunden: vent på, at den danske fil er gemt."""
        until = self.clock() + UPLOAD_WAIT
        while True:
            try:
                mtime = os.path.getmtime(cand.da_path)
            except OSError:
                mtime = None
            if mtime is not None and (before is None or mtime > before):
                return True
            if self._bazarr_has_danish(cand):
                return True
            if self.clock() >= until:
                return False
            self.sleep(POLL_SECONDS)

    def deliver(self, cand: Candidate, work: str, cues: list[Cue]) -> str:
        with open(work, "rb") as f:
            content = f.read()
        filename = os.path.splitext(os.path.basename(cand.video_path))[0] + ".da.srt"
        try:
            before = os.path.getmtime(cand.da_path)
        except OSError:
            before = None
        why = ""
        try:
            # Vores danske tekst er altid almindelig, aldrig HI - også når den engelske kilde var HI (trin B4f).
            self.bazarr.upload(cand.media_type, cand.media_id, cand.series_id, filename, content, UPLOAD_TIMEOUT)
            if self._wait_for_bazarr(cand, before):
                log.info("Uploadet til Bazarr: %s (Bazarr registrerer teksten)", filename)
                return UPLOADED
            why = f"Bazarr har ikke gemt filen efter {UPLOAD_WAIT:g} sekunder"
        except ApiError as e:
            why = str(e)
        log.warning(
            "ADVARSEL: Upload til Bazarr fejlede for %s (%s) - skriver %s selv og beder Bazarr scanne disken",
            cand.title,
            why,
            cand.da_path,
        )
        write_file_atomic(cand.da_path, cues)
        try:
            self.bazarr.scan_disk(cand.media_type, cand.media_id, cand.series_id)
        except ApiError as e:
            log.warning("Kunne heller ikke bede Bazarr scanne disken for %s: %s", cand.title, e)
            return "upload fejlede, fil skrevet (scan disk fejlede)"
        return "upload fejlede, fil skrevet og scan disk bedt om"
