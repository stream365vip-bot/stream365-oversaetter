"""Trin B8: Anthropic afviser kaldet pga. kontoen (udgiftsgrænse, ingen credits eller ugyldig nøgle).

Sådan et svar må aldrig bruge et forsøg op eller føre til en upload med engelsk tekst: titlen afbrydes uden at
ændre status, og runneren holder pause (se Runner.process_direct). Netværk, 5xx og timeout håndteres som før.
"""

from __future__ import annotations

# Tekster i Anthropics fejlbeskeder, der betyder, at kontoen (ikke kaldet) er problemet.
LIMIT_WORDS = ("usage limit",)
CREDIT_WORDS = ("credit balance", "insufficient credit", "insufficient_credit", "billing")
KEY_TYPES = ("authentication_error", "permission_error")
KEY_STATUS = (401, 403)

LIMIT = "udgiftsgrænse"
KEY = "nøglefejl"


class AccountBlocked(Exception):
    """Anthropic afviser alle kald for kontoen. kind: "udgiftsgrænse" eller "nøglefejl" (til pause-linjen)."""

    def __init__(self, reason: str, kind: str = LIMIT):
        super().__init__(reason)
        self.reason, self.kind = reason, kind


def classify(status: int | None, error_type: str, message: str) -> AccountBlocked | None:
    """AccountBlocked, hvis status/fejltype/besked betyder udgiftsgrænse, manglende credits eller ugyldig nøgle."""
    text = message.lower()
    if any(w in text for w in LIMIT_WORDS):
        return AccountBlocked("Anthropic melder, at udgiftsgrænsen er nået (usage limits)", LIMIT)
    if status == 402 or error_type == "billing_error" or any(w in text for w in CREDIT_WORDS):
        return AccountBlocked("Anthropic melder, at der ikke er flere credits (insufficient credit)", LIMIT)
    if status in KEY_STATUS or error_type in KEY_TYPES:
        what = f"HTTP {status}" if status in KEY_STATUS else error_type
        return AccountBlocked(f"Anthropic afviser nøglen ({what}) - tjek anthropic_api_key.txt", KEY)
    return None


def account_problem(e: BaseException) -> AccountBlocked | None:
    """Er undtagelsen fra Anthropic-klienten et kontoproblem? (anthropic.APIStatusError har status_code og body)."""
    if isinstance(e, AccountBlocked):
        return e
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    error_type = str(error.get("type") or "") if isinstance(error, dict) else ""
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    blocked = classify(status if isinstance(status, int) else None, error_type, f"{message} {e}")
    if blocked is None and type(e).__name__ in ("AuthenticationError", "PermissionDeniedError"):
        blocked = AccountBlocked(f"Anthropic afviser nøglen ({type(e).__name__}) - tjek anthropic_api_key.txt", KEY)
    return blocked
