"""Trin C4 (8.11.2026): knapperne i Fangarr.

- Overslag: hvor mange afsnit (pr. sæson) eller film mangler dansk, og hvad det ca. koster at oversætte dem.
- Oversæt nu: de valgte sæsoner/filmen lægges i tabellen manual. Hovedløkken behandler dem som nye (sprogprofil i
  Bazarr, gyldig søgning efter dansk først, derefter oversættelse) - samme vej som alt andet.
- Prøv igen: titlens række glemmes, og den lægges i manual, så den vælges igen i næste runde.
- Opgiv: titlen får status opgivet og vælges ikke igen (medmindre der kommer en ny fil).

Alt her kaldes fra den interne adresse (api.py) med sin egen databaseforbindelse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .clients import ApiError
from .db import Database, iso
from .selection import has_danish
from .tjenester import DANISH_AUDIO

FALLBACK_USD = {"episode": 0.54, "movie": 0.83}  # målt 1.-8. nov. 2026, bruges indtil der er nok tal
DONE = ("faerdig", "ikke_muligt")
BUSY = ("soeger", "soegt", "i_gang", "forsoeg_igen", "venter_paa_dansk")
LOCKED = ("faerdig", "i_gang")  # Prøv igen/Opgiv må ikke røre dem


class Conflict(Exception):
    """Handlingen passer ikke til titlens status (svar 409)."""


def avg_cost(db: Database, media_type: str, days: int = 30) -> float:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    row = db.conn.execute(
        "SELECT SUM(cost_usd), COUNT(DISTINCT item_key) FROM spend WHERE item_key LIKE ? AND ts >= ?",
        (f"{media_type}:%", since),
    ).fetchone()
    total, n = row[0] or 0.0, row[1] or 0
    return round(total / n, 4) if n >= 3 else FALLBACK_USD[media_type]


def _danish(bazarr_item: dict | None) -> str | None:
    """ "tekst"/"tale", hvis Bazarr ser en dansk tekst eller et dansk lydspor."""
    if not bazarr_item:
        return None
    if has_danish(bazarr_item.get("subtitles") or []):
        return "tekst"
    for a in bazarr_item.get("audio_language") or []:
        names = {str(a.get(k) or "").strip().lower() for k in ("code2", "code3", "name")}
        if names & DANISH_AUDIO:
            return "tale"
    return None


def _state(db: Database, key: str, bazarr_item: dict | None, manual: set[str]) -> str:
    """dansk, i_gang eller mangler."""
    row = db.get(key)
    status = row["status"] if row is not None else None
    if status in DONE or _danish(bazarr_item):
        return "dansk"
    if status in BUSY or key in manual:
        return "i_gang"
    return "mangler"


def _manual_keys(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT key FROM manual")}


def _budget(db: Database, cfg) -> dict:
    from .db import month_key

    month = db.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE month = ?", (month_key(),)).fetchone()
    return {"month_usd": round(float(month[0]), 2), "budget_usd": cfg.monthly_budget_usd, "usd_to_dkk": cfg.usd_to_dkk}


def series_overview(cfg, db: Database, sonarr, bazarr, series_id: int) -> dict:
    series = sonarr.series(series_id)
    episodes = [e for e in sonarr.episodes(series_id) if e.get("hasFile")]
    note = None
    try:
        baz = {int(x["sonarrEpisodeId"]): x for x in bazarr.series_episodes(series_id)}
    except ApiError as e:
        baz, note = {}, f"Bazarr kunne ikke spørges ({e}) - antallet er et skøn"
    manual = _manual_keys(db)
    price = avg_cost(db, "episode")
    seasons: dict[int, dict] = {}
    for e in episodes:
        n = int(e.get("seasonNumber") or 0)
        s = seasons.setdefault(n, {"season": n, "episodes": 0, "dansk": 0, "i_gang": 0, "mangler": 0, "keys": []})
        state = _state(db, f"episode:{int(e['id'])}", baz.get(int(e["id"])), manual)
        s["episodes"] += 1
        s[state] += 1
        if state == "mangler":
            s["keys"].append(int(e["id"]))
    out = []
    for n in sorted(seasons):
        s = seasons[n]
        s["est_usd"] = round(s["mangler"] * price, 2)
        s.pop("keys")
        out.append(s)
    return {
        "media": "series",
        "id": series_id,
        "title": series.get("title") or f"serie {series_id}",
        "year": series.get("year"),
        "seasons": out,
        "price_per_item_usd": price,
        "note": note,
        **_budget(db, cfg),
    }


def movie_overview(cfg, db: Database, radarr, bazarr, radarr_id: int) -> dict:
    movie = radarr.movie(radarr_id)
    note = None
    try:
        item = bazarr.movie(radarr_id)
    except ApiError as e:
        item, note = None, f"Bazarr kunne ikke spørges ({e})"
    has_file = bool(movie.get("hasFile"))
    state = _state(db, f"movie:{radarr_id}", item, _manual_keys(db)) if has_file else "ingen_fil"
    price = avg_cost(db, "movie")
    return {
        "media": "movie",
        "id": radarr_id,
        "title": movie.get("title") or f"film {radarr_id}",
        "year": movie.get("year"),
        "state": state,
        "est_usd": price if state == "mangler" else 0.0,
        "price_per_item_usd": price,
        "note": note,
        **_budget(db, cfg),
    }


def _add(db: Database, key: str, media_type: str, media_id: int, series_id, title: str, source: str) -> None:
    db.conn.execute(
        "INSERT INTO manual (key, media_type, media_id, series_id, title, added_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET added_at = excluded.added_at, source = excluded.source",
        (key, media_type, media_id, series_id, title, iso(datetime.now(UTC)), source),
    )


def translate_series(db: Database, sonarr, bazarr, series_id: int, seasons: list[int]) -> dict:
    """Læg afsnit uden dansk i de valgte sæsoner i manual. Returnerer antal."""
    series = sonarr.series(series_id)
    title = series.get("title") or f"serie {series_id}"
    wanted = {int(s) for s in seasons}
    try:
        baz = {int(x["sonarrEpisodeId"]): x for x in bazarr.series_episodes(series_id)}
    except ApiError:
        baz = {}
    manual = _manual_keys(db)
    added = 0
    for e in sonarr.episodes(series_id):
        if not e.get("hasFile") or int(e.get("seasonNumber") or 0) not in wanted:
            continue
        key = f"episode:{int(e['id'])}"
        if _state(db, key, baz.get(int(e["id"])), manual) != "mangler":
            continue
        name = f"{title} {int(e.get('seasonNumber') or 0)}x{int(e.get('episodeNumber') or 0):02d}"
        _add(db, key, "episode", int(e["id"]), series_id, name, "oversaet-nu")
        added += 1
    db.conn.commit()
    return {"added": added, "title": title}


def translate_movie(db: Database, radarr, bazarr, radarr_id: int) -> dict:
    movie = radarr.movie(radarr_id)
    title = movie.get("title") or f"film {radarr_id}"
    try:
        item = bazarr.movie(radarr_id)
    except ApiError:
        item = None
    key = f"movie:{radarr_id}"
    if not movie.get("hasFile") or _state(db, key, item, _manual_keys(db)) != "mangler":
        return {"added": 0, "title": title}
    _add(db, key, "movie", radarr_id, None, title, "oversaet-nu")
    db.conn.commit()
    return {"added": 1, "title": title}


def _parse_key(key: str) -> tuple[str, int]:
    kind, _, ident = key.partition(":")
    if kind not in ("movie", "episode") or not ident.isdigit():
        raise ValueError(f"ukendt nøgle {key!r}")
    return kind, int(ident)


def retry(db: Database, key: str) -> dict:
    kind, ident = _parse_key(key)
    row = db.get(key)
    if row is not None and row["status"] in LOCKED:
        raise Conflict(f"{row['title']} har status {row['status']} og kan ikke prøves igen herfra")
    title = (row["title"] if row is not None else None) or key
    series_id = row["series_id"] if row is not None else None
    if row is not None:
        db.delete(key)
    _add(db, key, kind, ident, series_id, title, "proev-igen")
    db.conn.commit()
    return {"key": key, "title": title}


def give_up(db: Database, key: str) -> dict:
    _parse_key(key)
    row = db.get(key)
    if row is None:
        raise Conflict(f"{key} findes ikke hos Oversætteren")
    if row["status"] in LOCKED:
        raise Conflict(f"{row['title']} har status {row['status']} og kan ikke opgives")
    db.upsert(key, status="opgivet", reason="opgivet i Fangarr")
    db.conn.execute("DELETE FROM manual WHERE key = ?", (key,))
    db.conn.commit()
    return {"key": key, "title": row["title"]}
