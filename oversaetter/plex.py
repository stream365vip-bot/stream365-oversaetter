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


def map_path(path: str, mapping: str) -> str:
    """PLEX_PATH_MAP="fra=til": byt præfikset fra ud med til (tom = stien er den samme i Plex)."""
    if "=" not in mapping:
        return path
    old, new = (part.strip() for part in mapping.split("=", 1))
    if old and (path == old or path.startswith(old.rstrip("/") + "/")):
        return new.rstrip("/") + path[len(old.rstrip("/")) :]
    return path


def find_section(sections: list[dict], folder: str) -> str | None:
    """Nøglen på det bibliotek, hvis Location indeholder mappen (den længste/mest præcise sti vinder)."""
    best, best_len = None, -1
    for section in sections:
        for location in section.get("Location") or []:
            root = (location.get("path") or "").rstrip("/")
            if root and (folder == root or folder.startswith(root + "/")) and len(root) > best_len:
                best, best_len = str(section.get("key")), len(root)
    return best


class Plex:
    def __init__(self, url: str, token: str, http: httpx.Client):
        self.base = url.rstrip("/")
        self.headers = {"X-Plex-Token": token, "Accept": "application/json"}
        self.http = http

    def _get(self, path: str, params=None) -> httpx.Response:
        r = self.http.get(self.base + path, params=params, headers=self.headers, timeout=httpx.Timeout(PLEX_TIMEOUT))
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
