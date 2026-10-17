"""Hård tidsgrænse på kald, der ellers kunne vente for evigt.

httpx' egen timeout gælder hver enkelt læsning, ikke hele kaldet: en server, der sender lidt ad gangen
(eller en forbindelse, der hænger), kan holde et kald i gang i timevis. Her køres kaldet i en baggrundstråd,
og der ventes højst det angivne antal sekunder på svaret.
"""

from __future__ import annotations

import threading


def call_with_timeout(fn, seconds: float, *args, **kwargs):
    """Kald fn(*args, **kwargs). Rejser TimeoutError, hvis der ikke er svar inden for seconds sekunder.

    Et kald, der overskrider grænsen, opgives (tråden får lov at køre færdig i baggrunden og bliver ikke ventet på).
    """
    box: dict = {}
    done = threading.Event()

    def run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:  # sendes videre til kalderen nedenfor
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=run, name="kald-med-tidsgraense", daemon=True).start()
    if not done.wait(max(0.0, seconds)):
        raise TimeoutError(f"intet svar inden for {seconds:g} sekunder")
    if "error" in box:
        raise box["error"]
    return box["value"]
