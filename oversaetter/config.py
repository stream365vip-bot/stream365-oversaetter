"""Indstillinger læses fra miljøvariabler (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass


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

    poll_minutes: float = 15.0
    seerr_delay_minutes: float = 60.0
    translate_tag: str = "oversaet"
    tag_delay_hours: float = 48.0
    translate_timeout_minutes: float = 45.0

    max_line_chars: int = 42
    proofread_batch: int = 50
    proofread_overlap: int = 5
    proofread_model: str = "claude-sonnet-5-5"
    proofread_effort: str = "medium"
    monthly_budget_usd: float = 5.0
    price_input_per_m: float = 2.0
    price_output_per_m: float = 10.0

    credit_text: str = "Oversat af Claude for ≋"
    dry_run: bool = True
    data_dir: str = "/data"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        cfg = cls()
        for field in cls.__dataclass_fields__:
            raw = env.get(field.upper())
            if raw is None or raw == "":
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
        return cfg

    def anthropic_api_key(self) -> str:
        try:
            with open(self.anthropic_api_key_file, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def price(self, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
        inp = self.price_input_per_m / 1_000_000
        out = self.price_output_per_m / 1_000_000
        return input_tokens * inp + cache_read * inp * 0.1 + cache_write * inp * 1.25 + output_tokens * out
