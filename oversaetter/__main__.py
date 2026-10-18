"""Start: `python -m oversaetter` (løkken), `python -m oversaetter test --en .. --da .. --out ..`
eller `python -m oversaetter oversaet-test --en .. --out .. --provider .. --model ..`."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import Config
from .db import Database
from .oversaet import PROVIDERS, run_translate_test
from .postprocess import make_client, postprocess
from .proofread import DbBudget
from .srt import read_file, write_file_atomic


class OneLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace("\r", " ").replace("\n", " | ")


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(OneLineFormatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("oversaetter").setLevel(level.upper())


class MemoryBudget:
    """Bruges i testtilstand hvis databasen ikke kan åbnes."""

    def __init__(self):
        self.total = 0.0

    def spent(self) -> float:
        return self.total

    def record(self, item_key, input_tokens, output_tokens, cost) -> None:
        self.total += cost


def run_test(cfg: Config, en: str, da: str, out: str, client=None, budget=None) -> int:
    log = logging.getLogger("oversaetter")
    paths = {os.path.abspath(p) for p in (en, da)}
    if os.path.abspath(out) in paths:
        log.error("--out må ikke være den samme fil som --en eller --da")
        return 2
    if os.path.exists(out):
        log.error("%s findes allerede - vælg et nyt filnavn", out)
        return 2
    if budget is None:
        try:
            budget = DbBudget(Database(os.path.join(cfg.data_dir, "oversaetter.db")))
        except Exception:
            budget = MemoryBudget()
    if client is None:
        client = make_client(cfg)
    result = postprocess(read_file(da), read_file(en), cfg, client, budget, item_key="test")
    write_file_atomic(out, result.cues)
    report = (
        "\n".join(
            [
                "Testrapport",
                f"Engelsk kilde: {en}",
                f"Dansk fil: {da}",
                f"Resultat: {out}",
                f"Flag før korrektur: {result.flags_before}",
                f"Flag efter korrektur: {result.flags_after}",
                f"Korrekturmodel: {result.model} (effort {cfg.proofread_effort})",
                f"Rettelser fra korrektur: {result.fixes}",
                f"Kasserede rettelser: {result.rejected}",
                f"Pris: {result.cost:.4f} USD",
                f"Korrektur: {result.proofread}",
                f"Kreditlinje: {result.credit}",
                "Stadig flaget: "
                + (", ".join(f"#{p + 1} {'/'.join(f)}" for p, f in result.remaining.items()) or "ingen"),
            ]
        )
        + "\n"
    )
    with open(out + ".rapport.txt", "w", encoding="utf-8") as f:
        f.write(report)
    print(report, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oversaetter")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="kør løkken (standard)")
    t = sub.add_parser("test", help="kør kun efterbehandlingen på to filer")
    t.add_argument("--en", required=True, help="engelsk .srt")
    t.add_argument("--da", required=True, help="dansk .srt")
    t.add_argument("--out", required=True, help="ny fil der skal skrives")
    o = sub.add_parser("oversaet-test", help="oversæt en hel engelsk fil i bidder (uden Lingarr og korrektur)")
    o.add_argument("--en", required=True, help="engelsk .srt")
    o.add_argument("--out", required=True, help="ny dansk fil der skal skrives")
    o.add_argument("--provider", required=True, choices=PROVIDERS)
    o.add_argument("--model", required=True, help="fx claude-sonnet-5-5, deepseek-v4-pro, gemini-3.8-flash")
    o.add_argument("--effort", choices=("low", "medium", "high"), help="ikke for deepseek")
    o.add_argument("--batch", type=int, default=50, help="replikker pr. bid (standard 50)")
    o.add_argument("--prompt", choices=("v3", "v4"), default="v3", help="instruksen (standard v3)")
    o.add_argument("--context", choices=("on", "off"), default="off", help="filmkontekst fra TMDB (kræver --tmdb-id)")
    o.add_argument("--glossary", choices=("on", "off"), default="off", help="ordliste pr. film før bidderne")
    o.add_argument("--tmdb-id", help="TMDB-id til konteksten, fx 12345 (film) eller tv:12345 (serie)")
    o.add_argument("--parallel", type=int, default=1, help="så mange bidder samtidig (standard 1)")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    if args.cmd == "test":
        return run_test(cfg, args.en, args.da, args.out)
    if args.cmd == "oversaet-test":
        return run_translate_test(
            cfg,
            args.en,
            args.out,
            args.provider,
            args.model,
            args.effort,
            args.batch,
            prompt=args.prompt,
            context=args.context == "on",
            glossary=args.glossary == "on",
            tmdb_id=args.tmdb_id,
            parallel=args.parallel,
        )
    from .runner import main as run_main

    run_main(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
