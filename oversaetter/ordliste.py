"""Ordliste pr. film (GLOSSARY=on): ét kald før bidderne med hele den engelske tekst og filmkonteksten.

Svaret er navne, kælenavne, øgenavne, steder, gentagne udtryk/slang med deres danske oversættelse, tiltale (du/De)
mellem personerne og tonen. Ordlisten sættes ind i systeminstruksen for alle bidder (så den caches), gemmes i
databasen og genbruges på tværs af afsnit i samme serie.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .checks import SWEARS_AND_EXCLAMATIONS

log = logging.getLogger("oversaetter")

MAX_TERMS = 60
MAX_MERGED = 80  # med termer fra tidligere afsnit af serien
MAX_INPUT_TOKENS = 30_000
CHARS_PER_TOKEN = 3.5  # forsigtigt skøn for engelsk undertekst

SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"en": {"type": "string"}, "da": {"type": "string"}, "note": {"type": "string"}},
                "required": ["en", "da", "note"],
                "additionalProperties": False,
            },
        },
        "address": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "who": {"type": "string"},
                    "to": {"type": "string"},
                    "form": {"type": "string", "enum": ["du", "De"]},
                },
                "required": ["who", "to", "form"],
                "additionalProperties": False,
            },
        },
        "tone": {"type": "string"},
    },
    "required": ["terms", "address", "tone"],
    "additionalProperties": False,
}

PROMPT = f"""You prepare the Danish subtitles for a film or episode. You get the film context (if known) and the \
whole English subtitle text. Make a glossary that a team of translators will follow so the whole file is consistent.
- "terms": at most {MAX_TERMS} names, nicknames, insults/epithets, places, organisations, recurring expressions and \
slang, each with how it must be written in Danish subtitles ("da") and a short "note" (e.g. "name, keep", \
"nickname", "slang: cool"). Names of people, places, companies and brands stay unchanged (da = en). Nicknames and \
epithets with a meaning are translated when a Danish viewer would otherwise miss the point. English insults, \
slang and forms of address ALWAYS get a Danish translation in the list (bitch/bitches -> kælling/kællinger, \
dude -> mand) and are never marked "keep". Only proper names (people, places, companies, groups, shows, brands) may \
stay untranslated. Organisations and places are written with the Danish spelling when one exists: Taliban -> \
Taleban, Hezbollah -> Hizbollah, Al Qaeda -> al-Qaeda, Tehran -> Teheran. Slang short forms of such names (e.g. \
"Tali") get the Danish name (Taleban), they are never kept. Swear words, exclamations and filler words are \
NEVER terms (shit, fuck, damn, hell, holy shit, \
Jesus, oh my God, okay, yeah, man, hey ...): the translators vary them by context.
- "address": for pairs of characters, how they address each other in Danish: "du" (normal) or "De" (only very \
formal: royalty, servants to masters, period drama).
- "tone": one line about tone and register (e.g. "rå teenagekomedie, meget bandeord").
If "previous_glossary" is given (earlier episodes of the same series), keep its translations unchanged.
Reply ONLY with JSON {{"terms":[{{"en":"..","da":"..","note":".."}}],"address":[{{"who":"..","to":"..",\
"form":"du"}}],"tone":".."}}."""


@dataclass
class Wordlist:
    terms: list[dict] = field(default_factory=list)  # {"en", "da", "note"}
    address: list[dict] = field(default_factory=list)  # {"who", "to", "form"}
    tone: str = ""
    cost: float | None = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_json(self) -> str:
        return json.dumps({"terms": self.terms, "address": self.address, "tone": self.tone}, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Wordlist:
        return parse(text)

    def block(self) -> str:
        """Teksten til systeminstruksen."""
        if not (self.terms or self.address or self.tone):
            return ""
        lines = ["Film glossary (use these Danish translations consistently in every cue):"]
        for t in self.terms:
            lines.append(f'- "{t["en"]}" -> "{t["da"]}"' + (f" ({t['note']})" if t.get("note") else ""))
        if self.address:
            lines.append("Forms of address (du/De):")
            lines += [f"- {a['who']} to {a['to']}: {a['form']}" for a in self.address]
        if self.tone:
            lines.append(f"Tone: {self.tone}")
        return "\n".join(lines)


def _text(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def is_swear_or_exclamation(en: str) -> bool:
    """Termen er et bandeord, udbrud eller fyldord (checks.SWEARS_AND_EXCLAMATIONS) - de filtreres fra (trin B4d)."""
    return " ".join(re.sub(r"[^\w' ]", " ", en.lower()).split()) in SWEARS_AND_EXCLAMATIONS


def parse(text: str) -> Wordlist:
    """Læs svaret (tåler ```json-hegn). Ugyldige termer og bandeord/udbrud springes over. Rejser ValueError ved
    ugyldig JSON."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("intet JSON-objekt i svaret")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("svaret er ikke et JSON-objekt")
    terms, seen = [], set()
    for t in data.get("terms") or []:
        if not isinstance(t, dict):
            continue
        en, da = _text(t.get("en")), _text(t.get("da"))
        if not en or not da or en.lower() in seen or is_swear_or_exclamation(en):
            continue
        seen.add(en.lower())
        terms.append({"en": en, "da": da, "note": _text(t.get("note"))})
    address = []
    for a in data.get("address") or []:
        if isinstance(a, dict) and _text(a.get("who")) and _text(a.get("to")) and a.get("form") in ("du", "De"):
            address.append({"who": _text(a["who"]), "to": _text(a["to"]), "form": a["form"]})
    return Wordlist(terms=terms[:MAX_TERMS], address=address[:MAX_TERMS], tone=_text(data.get("tone")))


def merge_previous(previous: list[str]) -> Wordlist:
    """Saml tidligere ordlister for serien (nyeste først) til én; den nyeste oversættelse af en term vinder."""
    out = Wordlist()
    seen_terms, seen_address = set(), set()
    for text in previous:
        try:
            wl = parse(text)
        except (ValueError, TypeError):
            continue
        for t in wl.terms:
            if t["en"].lower() not in seen_terms and len(out.terms) < MAX_MERGED:
                seen_terms.add(t["en"].lower())
                out.terms.append(t)
        for a in wl.address:
            key = (a["who"].lower(), a["to"].lower())
            if key not in seen_address:
                seen_address.add(key)
                out.address.append(a)
        out.tone = out.tone or wl.tone
    return out


def english_text(en: list[str]) -> str:
    """Hele den engelske tekst (én replik pr. linje), højst MAX_INPUT_TOKENS - ellers de første 2/3."""
    lines = [" / ".join(x.strip() for x in t.split("\n") if x.strip()) for t in en]
    text = "\n".join(lines)
    limit = int(MAX_INPUT_TOKENS * CHARS_PER_TOKEN)
    if len(text) > limit:
        text = "\n".join(lines[: max(1, len(lines) * 2 // 3)])
        log.info("Ordliste: teksten er for lang - bruger de første 2/3 af replikkerne")
        if len(text) > limit:
            text = text[:limit]
    return text


def build(
    ask,
    en: list[str],
    context: str | None,
    previous: Wordlist | None,
    on_usage=None,
    speakers: list[str] | None = None,
    notes: list[str] | None = None,
) -> Wordlist | None:
    """Lav ordlisten med ét kald. ask(system, user, schema) -> Reply. Returnerer None (med en WARNING) ved fejl.

    speakers: talernavne fra en HI-kilde (trin B4); gives med sammen med rollelisten i konteksten. notes: de engelske
    replikker med talernavnet som note ("[Rick] Let's go."), så modellen kan se, hvem der taler til hvem.
    """
    parts = [context] if context else []
    if previous is not None and (previous.terms or previous.address):
        parts.append("previous_glossary (earlier episodes of this series):\n" + previous.to_json())
    if speakers:
        parts.append('Speakers named in the subtitles (use them with the cast for "address"): ' + ", ".join(speakers))
        parts.append("English subtitles (one cue per line, [Name] = who speaks):\n" + english_text(notes or en))
    else:
        parts.append("English subtitles (one cue per line):\n" + english_text(en))
    user = "\n\n".join(parts)
    try:
        reply = ask(PROMPT, user, SCHEMA)
    except Exception as e:  # timeout, netværk, API-fejl
        log.warning("Ordlisten kunne ikke laves (%s: %s) - oversætter uden ordliste", type(e).__name__, str(e)[:200])
        return None
    if on_usage is not None:
        on_usage(reply.input_tokens, reply.output_tokens)
    if reply.stop != "ok":
        log.warning("Ordlisten blev afbrudt (stop=%s) - oversætter uden ordliste", reply.stop)
        return None
    try:
        wl = parse(reply.text)
    except (ValueError, TypeError) as e:
        log.warning("Ugyldigt svar på ordlisten (%s) - oversætter uden ordliste", e)
        return None
    wl.input_tokens, wl.output_tokens = reply.input_tokens, reply.output_tokens
    if previous is not None:
        known = {t["en"].lower() for t in wl.terms}
        for t in previous.terms:
            if len(wl.terms) >= MAX_MERGED:
                break
            if t["en"].lower() not in known:
                wl.terms.append(t)
                known.add(t["en"].lower())
        pairs = {(a["who"].lower(), a["to"].lower()) for a in wl.address}
        wl.address += [a for a in previous.address if (a["who"].lower(), a["to"].lower()) not in pairs]
        wl.tone = wl.tone or previous.tone
    log.info("Ordliste: %d termer, %d tiltaler", len(wl.terms), len(wl.address))
    return wl
