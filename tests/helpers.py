"""Fælles hjælpere til testene. Alle tekster er selvskrevne."""

from __future__ import annotations

import json
from types import SimpleNamespace

from oversaetter.config import Config
from oversaetter.srt import Cue


def srt(*cues: tuple[str, str, str]) -> str:
    """srt(("00:00:01,000", "00:00:02,000", "Hej"), ...) -> SRT-tekst."""
    blocks = [f"{n}\n{a} --> {b}\n{t}\n" for n, (a, b, t) in enumerate(cues, start=1)]
    return "\n".join(blocks)


def cue(start_s: float, end_s: float, text: str) -> Cue:
    return Cue(int(start_s * 1000), int(end_s * 1000), text)


def system_text(system) -> str:
    """Systeminstruksen som tekst - også når den er sendt som blok til prompt-cachen (PROMPT_CACHE=true)."""
    return system if isinstance(system, str) else "".join(b["text"] for b in system)


def cfg(**kw) -> Config:
    c = Config(data_dir="/nonexistent", anthropic_api_key_file="/nonexistent")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class FakeBudget:
    def __init__(self, spent: float = 0.0):
        self.total = spent
        self.calls = 0

    def spent(self) -> float:
        return self.total

    def record(self, item_key, input_tokens, output_tokens, cost) -> None:
        self.total += cost
        self.calls += 1


def response(fixes=None, text=None, stop="end_turn", inp=1000, out=500):
    body = text if text is not None else json.dumps({"fixes": fixes or []}, ensure_ascii=False)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=body)],
        stop_reason=stop,
        usage=SimpleNamespace(
            input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0
        ),
    )


class FakeClaude:
    """Falsk Anthropic-klient: svarer med de givne svar i rækkefølge, eller kalder en funktion."""

    def __init__(self, replies):
        self.replies = list(replies) if not callable(replies) else replies
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if callable(self.replies):
            return self.replies(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def lines(self, n: int = -1) -> list[dict]:
        content = self.requests[n]["messages"][0]["content"]
        return json.loads(content[content.index("{") :])["lines"]
