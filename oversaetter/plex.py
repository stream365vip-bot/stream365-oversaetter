"""Trin B4g: bed Plex scanne filmens/afsnittets mappe, når vores danske tekst er uploadet til Bazarr.

Bazarrs egen Plex-opdatering kører kun ved download, ikke ved upload. Derfor finder vi selv det Plex-bibliotek,
der indeholder mappen (GET /library/sections, match på Location-stien), og beder om en delvis scanning af netop
mappen (GET /library/sections/<id>/refresh?path=<mappe>). Tokenet sendes kun i X-Plex-Token-headeren og logges
aldrig. Kaldet må aldrig blokere eller fejle oversættelsen: hård timeout på 15 sekunder, og alle fejl bliver til en
advarsel.
"""

from __future__ import annotations

import logging
import os

import httpx2 as httpx

from .config import Config, read_key
from .deadline import call_with_timeout

log = logging.getLogger("oversaetter")

PLEX_TIMEOUT = 15.0  # højst så mange sekunder på hele Plex-trinnet (begge kald)
LIST_TIMEOUT = 60.0  # trin C5: højst så mange sekunder pr. side, når bibliotekets filer hentes
RECENT_ITEMS = 300  # trin C5: først ses der kun på de senest tilføjede (billigt)
PAGE_ITEMS = 1000  # trin C5: sidestørrelse, når hele biblioteket må hentes


def map_path(path: str, mapping: str) -> str:
    """PLEX_PATH_MAP="fra=til": byt præfikset fra ud med til (tom = stien er den samme i Plex)."""
    if "=" not in mapping:
        return path
    old, new = (part.strip() for part in mapping.split("=", 1))
    if old and (path == old or path.startswith(old.rstrip("/") + "/")):
        return new.rstrip("/") + path[len(old.rstrip("/")) :]
    return path


def section_for(sections: list[dict], folder: str) -> dict | None:
    """Det bibliotek, hvis Location indeholder mappen (den længste/mest præcise sti vinder)."""
    best, best_len = None, -1
    for section in sections:
        for location in section.get("Location") or []:
            root = (location.get("path") or "").rstrip("/")
            if root and (folder == root or folder.startswith(root + "/")) and len(root) > best_len:
                best, best_len = section, len(root)
    return best


def find_section(sections: list[dict], folder: str) -> str | None:
    """Nøglen på det bibliotek, der indeholder mappen."""
    section = section_for(sections, folder)
    return None if section is None else str(section.get("key"))


class Plex:
    def __init__(self, url: str, token: str, http: httpx.Client):
        self.base = url.rstrip("/")
        self.headers = {"X-Plex-Token": token, "Accept": "application/json"}
        self.http = http

    def _get(self, path: str, params=None, timeout: float = PLEX_TIMEOUT) -> httpx.Response:
        r = self.http.get(self.base + path, params=params, headers=self.headers, timeout=httpx.Timeout(timeout))
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} på {path}")
        return r

    def sections(self) -> list[dict]:
        try:
            return self._get("/library/sections").json()["MediaContainer"].get("Directory") or []
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RuntimeError("/library/sections svarede ikke med gyldige data") from None

    def refresh(self, folder: str) -> str:
        """Bed om delvis scanning af mappen. Returnerer bibliotekets nøgle."""
        key = find_section(self.sections(), folder)
        if key is None:
            raise RuntimeError("intet bibliotek i Plex indeholder mappen")
        self._get(f"/library/sections/{key}/refresh", {"path": folder})
        return key

    def files(self, key: str, kind: int, start: int, size: int, sort: str | None = None) -> tuple[set[str], int]:
        """Filerne bag en side af bibliotekets film (kind 1) eller afsnit (kind 4) + antal titler på siden."""
        params = {"type": kind, "X-Plex-Container-Start": start, "X-Plex-Container-Size": size}
        if sort:
            params["sort"] = sort
        try:
            items = self._get(f"/library/sections/{key}/all", params, LIST_TIMEOUT).json()["MediaContainer"]
            items = items.get("Metadata") or []
            parts = [p for v in items for m in v.get("Media") or [] for p in m.get("Part") or []]
            return {p["file"] for p in parts if p.get("file")}, len(items)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RuntimeError(f"/library/sections/{key}/all svarede ikke med gyldige data") from None


class Library:
    """Trin C5: findes en fil i Plex? Plex kan ikke søge på en sti, så bibliotekets filer hentes: først kun de
    senest tilføjede titler (nok til nye film og afsnit), og kun hvis filen ikke er dér, hele biblioteket (nødvendigt
    ved opgraderinger, hvor titlen er gammel). Svarene gemmes, så flere filer kan tjekkes i samme omgang.
    Kaster en fejl, når Plex ikke svarer - så ved vi ikke, om filen er der.
    """

    def __init__(self, plex: Plex):
        self.plex = plex
        self._sections: list[dict] | None = None
        self._recent: dict[str, set[str]] = {}
        self._all: dict[str, set[str]] = {}

    def has(self, video: str) -> bool:
        if self._sections is None:
            self._sections = self.plex.sections()
        section = section_for(self._sections, os.path.dirname(video))
        if section is None:
            raise RuntimeError("intet bibliotek i Plex indeholder mappen")
        key, kind = str(section.get("key")), 4 if section.get("type") == "show" else 1
        if key not in self._recent:
            self._recent[key] = self.plex.files(key, kind, 0, RECENT_ITEMS, "addedAt:desc")[0]
        if video in self._recent[key]:
            return True
        if key not in self._all:
            found: set[str] = set()
            start = 0
            while True:
                files, count = self.plex.files(key, kind, start, PAGE_ITEMS)
                found |= files
                if count < PAGE_ITEMS:
                    break
                start += PAGE_ITEMS
            self._all[key] = found
        return video in self._all[key]


def library(cfg: Config, http: httpx.Client) -> Library | None:
    """Et opslag i Plex til én omgang kontrol, eller None hvis PLEX_URL eller tokenet mangler."""
    token = read_key(cfg.plex_token_file)
    if not cfg.plex_url or not token:
        return None
    return Library(Plex(cfg.plex_url, token, http))


def refresh_folder(cfg: Config, http: httpx.Client, video_path: str) -> bool | None:
    """Bed Plex scanne videoens mappe. True = bedt om, False = fejlede (advarsel), None = ikke sat op. Fejler aldrig."""
    token = read_key(cfg.plex_token_file)
    if not cfg.plex_url or not token:
        log.info("Plex: PLEX_URL eller tokenet i %s mangler - beder ikke Plex scanne", cfg.plex_token_file)
        return None
    folder = map_path(os.path.dirname(video_path), cfg.plex_path_map)
    try:
        call_with_timeout(Plex(cfg.plex_url, token, http).refresh, PLEX_TIMEOUT, folder)
    except TimeoutError:
        log.warning("Plex: intet svar inden for %g sekunder - kunne ikke bede om scanning af %s", PLEX_TIMEOUT, folder)
        return False
    except Exception as e:
        # Egen tekst ved forbindelsesfejl, så httpx' beskeder ikke kommer i loggen.
        why = f"kunne ikke forbinde ({type(e).__name__})" if isinstance(e, httpx.HTTPError) else str(e)
        log.warning("Plex: kunne ikke bede om scanning af %s (%s)", folder, why)
        return False
    log.info("Plex: bad om scanning af %s", folder)
    return True
