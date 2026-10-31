"""Trin B7: send hver færdig eller fejlet oversættelse til appens backend, så en RSS-widget kan vise de seneste.

Når en titel ender som "Færdig" eller "fejlet" i driften, sendes ét POST til FEED_URL med headeren
X-Oversaetter-Token (fra FEED_TOKEN_FILE) og en lille JSON-krop. Tokenet logges aldrig. Sendingen må aldrig blokere
eller fejle oversættelsen: hård tidsgrænse på 10 sekunder pr. forsøg, ét genforsøg, og alle fejl bliver til en
WARNING. Tom FEED_URL = slået fra. oversaet-test sender aldrig (den bruger ikke runneren).
"""

from __future__ import annotations

import logging
import re

import httpx2 as httpx

from .config import Config, read_key
from .deadline import call_with_timeout

log = logging.getLogger("oversaetter")

FEED_TIMEOUT = 10.0  # højst så mange sekunder pr. forsøg
RETRY_PAUSE = 2.0  # sekunder mellem første forsøg og genforsøget
TOKEN_HEADER = "X-Oversaetter-Token"
# Bazarrs afsnitsnumre står bag serietitlen i vores titel, fx "Serie 5x02".
EPISODE_RE = re.compile(r"^(?P<title>.*\S)\s+(?P<season>\d+)x(?P<episode>\d+)$")


def split_episode(title: str) -> tuple[str, str]:
    """ "Serie 5x02" -> ("Serie", "S5E2"). Uden afsnitsnummer: (titlen, "")."""
    m = EPISODE_RE.match(title.strip())
    if not m:
        return title.strip(), ""
    return m["title"], f"S{int(m['season'])}E{int(m['episode'])}"


def build_payload(
    cfg: Config,
    title: str,
    media_type: str,
    status: str,
    cues: int,
    seconds: float,
    cost_usd: float,
    note: str,
) -> dict:
    """JSON-kroppen til backenden. status: "done" eller "failed"."""
    episode = media_type == "episode"
    name, label = split_episode(title) if episode else (title, "")
    payload = {
        "title": name,
        "media_type": "episode" if episode else "movie",
        "status": status,
        "cues": int(cues or 0),
        "minutes": int(round(max(0.0, seconds or 0.0) / 60)),
        "cost_usd": round(cost_usd or 0.0, 4),
        "cost_dkk": round((cost_usd or 0.0) * cfg.usd_to_dkk, 2),
        "note": note or "",
    }
    if episode:
        payload["episode_label"] = label
    return payload


def _post(http: httpx.Client, url: str, token: str, payload: dict) -> int:
    r = http.post(url, json=payload, headers={TOKEN_HEADER: token}, timeout=httpx.Timeout(FEED_TIMEOUT))
    return r.status_code


def _attempt(http: httpx.Client, url: str, token: str, payload: dict) -> tuple[str | None, bool]:
    """Ét forsøg. Returnerer (None = sendt ellers en kort dansk forklaring, om et genforsøg giver mening)."""
    try:
        code = call_with_timeout(_post, FEED_TIMEOUT, http, url, token, payload)
    except TimeoutError:
        return f"intet svar inden for {FEED_TIMEOUT:g} sekunder", True
    except httpx.HTTPError as e:  # egen tekst, så httpx' beskeder ikke kommer i loggen
        return f"kunne ikke forbinde ({type(e).__name__})", True
    if code < 300:
        return None, False
    if code in (401, 403):
        return f"HTTP {code}", False  # forkert token: et genforsøg hjælper ikke
    return f"HTTP {code}", True


def send(cfg: Config, http: httpx.Client, payload: dict, sleep=None) -> bool | None:
    """Send til FEED_URL. True = sendt, False = fejlede (WARNING), None = slået fra. Fejler aldrig."""
    if not cfg.feed_url:
        return None
    title = payload.get("title", "")
    if payload.get("episode_label"):
        title = f"{title} {payload['episode_label']}"
    try:
        token = read_key(cfg.feed_token_file)
        if not token:
            log.warning("Feed: intet token i %s - %s sendes ikke", cfg.feed_token_file, title)
            return False
        why, retry = _attempt(http, cfg.feed_url, token, payload)
        if why is not None and retry:
            # Ét genforsøg ved netværksfejl, timeout og serverfejl (ikke ved et forkert token).
            if sleep is not None:
                sleep(RETRY_PAUSE)
            why, _ = _attempt(http, cfg.feed_url, token, payload)
    except Exception as e:  # sikkerhedsnet: sendingen må aldrig fejle oversættelsen
        why = f"{type(e).__name__}: {e}"
    if why is not None:
        if why in ("HTTP 401", "HTTP 403"):
            why += f" - tjek at tokenet i {cfg.feed_token_file} er det samme som OVERSAETTER_TOKEN i backenden"
        log.warning("Feed: kunne ikke sende %s (%s)", title, why)
        return False
    log.info("Feed: sendt %s", title)
    return True
