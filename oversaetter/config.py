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
# Felter hvor en tom værdi i .env betyder "slået fra" i stedet for "brug standard".
EMPTY_ALLOWED = ("credit_text",)

# Priser i USD pr. million tokens (ind, ud) til testtilstanden oversaet-test. Ukendt model = "pris ukendt".
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "deepseek-v4-pro": (0.66, 1.98),
    "deepseek-flash": (0.15, 0.60),
    "gemini-3.8-flash": (0.75, 3.75),
}


def model_price(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Pris i USD for en model fra MODEL_PRICES, eller None hvis modellen er ukendt."""
    prices = MODEL_PRICES.get(model)
    if prices is None:
        return None
    return input_tokens * prices[0] / 1_000_000 + output_tokens * prices[1] / 1_000_000


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
        for name in ("bazarr_url", "radarr_url", "sonarr_url", "seerr_url"):
            setattr(cfg, name, getattr(cfg, name).rstrip("/"))
        cfg.translate_mode = cfg.translate_mode.strip().lower()
        if cfg.translate_mode not in TRANSLATE_MODES:
            raise ValueError(f"TRANSLATE_MODE={cfg.translate_mode!r} i .env er ukendt - brug direct eller lingarr")
        return cfg

    @property
    def direct(self) -> bool:
        return self.translate_mode == "direct"

    def anthropic_api_key(self) -> str:
        return read_key(self.anthropic_api_key_file)

    def bad_words(self) -> frozenset[str]:
        from .checks import known_bad_words

        return known_bad_words(self.known_bad_words)

    def price(self, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
        inp = self.price_input_per_m / 1_000_000
        out = self.price_output_per_m / 1_000_000
        return input_tokens * inp + cache_read * inp * 0.1 + cache_write * inp * 1.25 + output_tokens * out
