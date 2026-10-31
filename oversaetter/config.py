"""Indstillinger læses fra miljøvariabler (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Nøgler fra ældre versioner. Står de i .env, ignoreres de (og det logges én gang ved start).
OLD_KEYS = ("SEERR_DELAY_MINUTES", "TAG_DELAY_HOURS", "FIRST_SEEN_DELAY_HOURS")
# Korrektur-nøgler fra Lingarr-flowet. I TRANSLATE_MODE=direct ignoreres de (og det logges én gang ved start).
PROOFREAD_KEYS = (
    "PROOFREAD_MODEL",
    "PROOFREAD_EFFORT",
    "PROOFREAD_BATCH",
    "PROOFREAD_OVERLAP",
    "PROOFREAD_CALL_TIMEOUT_MINUTES",
)
TRANSLATE_MODES = ("direct", "lingarr")
PROMPT_VERSIONS = ("v3", "v4")
# Felter hvor en tom værdi i .env betyder "slået fra" i stedet for "brug standard".
EMPTY_ALLOWED = ("credit_text", "feed_url", "status_url", "qbittorrent_url", "status_mount_file", "status_docker")

# Priser i USD pr. million tokens (ind, ud) til testtilstanden oversaet-test. Ukendt model = "pris ukendt".
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "deepseek-v4-pro": (0.66, 1.98),
    "deepseek-flash": (0.15, 0.60),
    "gemini-3.8-flash": (0.75, 3.75),
}


# Prompt-cache (Anthropic): cache-læsning koster 10 % og cache-skrivning 125 % af inputprisen.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25
# Message Batches (Anthropic): 50 % af prisen på alle tokens, også prompt-cachens (--batch-api og BATCH_API=true).
BATCH_FACTOR = 0.5


def model_price(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    factor: float = 1.0,
) -> float | None:
    """Pris i USD for en model fra MODEL_PRICES, eller None hvis modellen er ukendt.

    input_tokens er de almindelige (ikke-cachede) tokens ind; cache_read/cache_write er prompt-cachens tokens.
    factor ganges på hele prisen (BATCH_FACTOR for svar fra en Message Batch).
    """
    prices = MODEL_PRICES.get(model)
    if prices is None:
        return None
    inp = prices[0] / 1_000_000
    return factor * (
        input_tokens * inp
        + cache_read * inp * CACHE_READ_FACTOR
        + cache_write * inp * CACHE_WRITE_FACTOR
        + output_tokens * prices[1] / 1_000_000
    )


def read_key(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:  # findes ikke, ingen adgang, eller en mappe (Docker laver en mappe, hvis filen mangler)
        return ""


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "ja", "on")


@dataclass
class Config:
    bazarr_url: str = "http://bazarr:6767"
    bazarr_api_key: str = ""
    radarr_url: str = "http://radarr:7878"
    radarr_api_key: str = ""
    sonarr_url: str = "http://sonarr:8989"
    sonarr_api_key: str = ""
    seerr_url: str = ""
    seerr_api_key: str = ""
    anthropic_api_key_file: str = "/run/secrets/anthropic_api_key"
    # Kun til testtilstanden oversaet-test. Mangler filerne, påvirker det ikke driften.
    deepseek_api_key_file: str = "/run/secrets/deepseek_api_key"
    gemini_api_key_file: str = "/run/secrets/gemini_api_key"
    # Valgfri TMDB-nøgle til filmkonteksten (CONTEXT=on). Mangler den, bruges kun Radarr/Sonarr.
    tmdb_api_key_file: str = "/run/secrets/tmdb_api_key"
    tmdb_api_key: str = ""
    tmdb_url: str = "https://api.themoviedb.org/3"
    # Trin B4g: valgfri Plex-scanning af mappen efter upload til Bazarr. Mangler URL eller token, springes den over.
    plex_url: str = ""
    plex_token_file: str = "/run/secrets/plex_token"
    plex_path_map: str = ""  # "fra=til": byt et sti-præfiks ud, hvis Plex ser mapperne et andet sted
    # Trin B6b: bed Bazarr synkronisere den danske tekst mod lyden efter upload (før Plex-scanningen).
    sync_after_upload: bool = True
    # Trin B7: send hver færdig eller fejlet oversættelse til appens backend (RSS-widget). Tom FEED_URL = slået fra.
    feed_url: str = ""
    feed_token_file: str = "/run/secrets/feed_token"
    usd_to_dkk: float = 6.7  # cost_dkk = cost_usd × USD_TO_DKK
    # Trin B11: serverstatus til appens backend hvert 5. minut (samme token som feedet). Tom STATUS_URL = slået fra.
    status_url: str = ""
    status_containers: str = "plex,bazarr,radarr,sonarr2,qbittorrent,autoscan,seerr"
    status_docker: str = ""  # docker-socket-proxy (http://...) til containers_down; tom = feltet udelades
    status_disk_path: str = "/"
    status_unionfs_path: str = "/mnt/unionfs"
    status_mount_file: str = ""  # tom = mappen <unionfs>/Media skal kunne læses
    status_plex: bool = False  # plex_streams/plex_transcodes/plex_bandwidth_mbps - til, når backenden kender dem
    qbittorrent_url: str = ""
    qbittorrent_file: str = "/run/secrets/qbittorrent"  # "bruger:kode"
    # Trin B8: Anthropic afviser kontoen (udgiftsgrænse, ingen credits, ugyldig nøgle) - så længe holdes der pause.
    account_pause_minutes: float = 30.0
    # Trin B9: titler på en dansk streamingtjeneste (TMDB, DK) venter så mange dage på en officiel dansk tekst.
    provider_wait: bool = True
    provider_wait_days_movies: float = 7.0
    provider_wait_days_series: float = 3.0

    poll_minutes: float = 15.0
    translate_tag: str = "oversaet"
    search_timeout_minutes: float = 10.0
    max_searches_per_round: int = 10
    translate_timeout_minutes: float = 45.0  # lingarr: ventetid på Bazarr; direct: højst så længe pr. emne i alt

    # direct (standard): programmet oversætter selv med Claude i bidder. lingarr: det gamle flow via Bazarr/Lingarr.
    translate_mode: str = "direct"
    translate_model: str = "claude-opus-5-5"  # valgt 18.10.2026 (se README)
    translate_effort: str = "medium"
    fallback_model: str = "claude-sonnet-5-5"  # bruges kun, når en bid afvises (refusal) også efter halvering
    max_fix_lines: int = 60  # højst så mange flagede replikker i rettelsesrunden
    # Forbedringer fra 18.10.2026 - alle slået fra, indtil Jesper har set en test (se README).
    prompt_version: str = "v3"  # v3 (standard) eller v4
    context: bool = False  # CONTEXT=on: filmkontekst (titel, handling, roller) i instruksen
    glossary: bool = False  # GLOSSARY=on: ordliste pr. film før bidderne
    parallel: int = 1  # så mange bidder sendes samtidig
    # Trin B2 (18.10.2026)
    reading_speed_max: float = 25.0  # tegn pr. sekund; reading_speed er kun et informationsflag
    prompt_cache: bool = True  # PROMPT_CACHE: systeminstruksen markeres til Anthropics prompt-cache
    # Trin B4c (18.10.2026): linjer, der starter eller slutter med ♪/#, fjernes fra den engelske kilde (sangtekst)
    drop_lyrics: bool = True
    # Trin B6 (19.10.2026): bid 2 og frem som én Anthropic Message Batch (halv pris). Standard fra.
    batch_api: bool = False
    batch_timeout_minutes: float = 90.0  # så længe ventes der på batchen, før den aflyses og resten oversættes direkte

    max_line_chars: int = 42
    proofread_batch: int = 50
    proofread_overlap: int = 5
    proofread_model: str = "claude-opus-5-5"
    proofread_effort: str = "medium"
    proofread_call_timeout_minutes: float = 10.0  # højst så længe på ét svar fra korrekturmodellen
    postprocess_timeout_minutes: float = 30.0  # højst så længe på efterbehandlingen af ét emne i alt
    monthly_budget_usd: float = 5.0
    price_input_per_m: float = 4.0
    price_output_per_m: float = 20.0
    known_bad_words: str = ""  # kommaseparerede ord, lægges oven i startlisten i checks.py

    # \n (backslash + n) bliver til et linjeskift i kreditlinjen. Valgt af Jesper 17.10.2026.
    credit_text: str = (
        'Oversat til dig af\\n<font color="#58D9AE">≋ </font><b>Stream<font color="#58D9AE">365</font></b>'
    )
    dry_run: bool = True
    data_dir: str = "/data"
    log_level: str = "INFO"
    ignored_keys: tuple[str, ...] = ()  # gamle nøgler fra .env, der ikke bruges længere
    proofread_keys: tuple[str, ...] = ()  # PROOFREAD_*-nøgler fra .env (ignoreres i direct-tilstand)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        cfg = cls()
        cfg.ignored_keys = tuple(k for k in OLD_KEYS if env.get(k, "").strip())
        cfg.proofread_keys = tuple(k for k in PROOFREAD_KEYS if env.get(k, "").strip())
        for field in cls.__dataclass_fields__:
            if field in ("ignored_keys", "proofread_keys"):
                continue
            raw = env.get(field.upper())
            if raw is None or (raw == "" and field not in EMPTY_ALLOWED):
                continue
            kind = type(getattr(cfg, field))
            if kind is bool:
                value = _bool(raw)
            elif kind is int:
                value = int(raw)
            elif kind is float:
                value = float(raw.replace(",", "."))
            else:
                value = raw
            setattr(cfg, field, value)
        for name in ("bazarr_url", "radarr_url", "sonarr_url", "seerr_url", "plex_url", "qbittorrent_url"):
            setattr(cfg, name, getattr(cfg, name).rstrip("/"))
        cfg.feed_url = cfg.feed_url.strip()
        cfg.status_url = cfg.status_url.strip()
        cfg.translate_mode = cfg.translate_mode.strip().lower()
        if cfg.translate_mode not in TRANSLATE_MODES:
            raise ValueError(f"TRANSLATE_MODE={cfg.translate_mode!r} i .env er ukendt - brug direct eller lingarr")
        cfg.prompt_version = cfg.prompt_version.strip().lower()
        if cfg.prompt_version not in PROMPT_VERSIONS:
            raise ValueError(f"PROMPT_VERSION={cfg.prompt_version!r} i .env er ukendt - brug v3 eller v4")
        cfg.parallel = max(1, cfg.parallel)
        return cfg

    @property
    def direct(self) -> bool:
        return self.translate_mode == "direct"

    def anthropic_api_key(self) -> str:
        return read_key(self.anthropic_api_key_file)

    def tmdb_key(self) -> str:
        return self.tmdb_api_key.strip() or read_key(self.tmdb_api_key_file)

    def switches(self) -> str:
        """Kort tekst med kontakterne fra 18.10.2026 til loggen og rapporten."""
        on = {True: "on", False: "off"}
        return (
            f"prompt {self.prompt_version}, kontekst {on[self.context]}, ordliste {on[self.glossary]}, "
            f"parallel {self.parallel}"
        )

    def bad_words(self) -> frozenset[str]:
        from .checks import known_bad_words

        return known_bad_words(self.known_bad_words)

    def price(self, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
        inp = self.price_input_per_m / 1_000_000
        out = self.price_output_per_m / 1_000_000
        return (
            input_tokens * inp
            + cache_read * inp * CACHE_READ_FACTOR
            + cache_write * inp * CACHE_WRITE_FACTOR
            + output_tokens * out
        )
