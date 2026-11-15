"""Trin C5 (15.11.2026): efter scanningen tjekkes det, at filen faktisk kom i Plex - ellers bedes Plex scanne igen.

Baggrund: fem NORDiC-film blev vist inden for et minut efter importen, men Plex' server kunne endnu ikke se de nye
mapper, så scanningen fandt ingenting, og filmene kom aldrig i Plex. Alt med fakes; intet netværk.
"""

import logging
import os
from datetime import timedelta

import httpx2

from oversaetter import api
from oversaetter.plex import Library, Plex
from tests.test_direct import FakeCommands
from tests.test_trin_b4g import FakePlex, with_plex
from tests.test_trin_c3 import NOW, arr, episode, make, movie, refreshes, rows  # noqa: F401

DANISH = dict(audio=("dan",))


class PlexWithFiles(FakePlex):
    """FakePlex, der også svarer på /library/sections/<id>/all med de filer, vi har lagt i biblioteket."""

    def __init__(self, locations, kinds=None):
        super().__init__(locations)
        self.kinds = kinds or {}
        self.files: dict[str, list[str]] = {key: [] for key in locations}  # ældste først
        self.broken = False

    def handle(self, req):
        if req.url.path == "/library/sections" and not self.fail:
            resp = super().handle(req)
            data = resp.json()
            for d in data["MediaContainer"]["Directory"]:
                d["type"] = self.kinds.get(d["key"], "movie")
            return httpx2.Response(200, json=data)
        if req.url.path.endswith("/all") and not self.fail:
            resp = super().handle(req)  # gemmer kaldet (og svarer 404)
            if self.broken:
                return resp
            _, q, _ = self.calls[-1]
            files = self.files[req.url.path.split("/")[3]]
            if q.get("sort") == "addedAt:desc":
                files = files[::-1]
            start, size = int(q["X-Plex-Container-Start"]), int(q["X-Plex-Container-Size"])
            items = [{"Media": [{"Part": [{"file": f}]}]} for f in files[start : start + size]]
            return httpx2.Response(200, json={"MediaContainer": {"size": len(items), "Metadata": items}})
        return super().handle(req)


def setup(arr, tmp_path, kinds=None, **kw):  # noqa: F811
    fake = PlexWithFiles({"1": [str(tmp_path / "Media")]}, kinds)
    with_plex(arr, fake)
    r, db, clock = make(arr, tmp_path, commands=FakeCommands(**DANISH), **kw)
    return fake, r, db, clock


def later(clock, **kw):
    clock["now"] += timedelta(**kw)


def listings(fake):
    return [c for c in fake.calls if c[0].endswith("/all")]


def test_found_at_first_check_is_confirmed_without_a_new_scan(arr, tmp_path):  # noqa: F811
    video = movie(arr, 1)
    fake, r, db, clock = setup(arr, tmp_path)
    assert r.run_once() == 1
    row = rows(db)["movie:1"]
    assert row["status"] == "vist" and row["plex_state"] == "tjekkes" and row["plex_tries"] == 0
    r.run_once()
    assert listings(fake) == []  # ingen kontrol før der er gået 2 minutter
    fake.files["1"].append(video)  # scanningen virkede
    later(clock, minutes=2)
    r.run_once()
    row = rows(db)["movie:1"]
    assert row["plex_state"] == "ok" and row["plex_next"] is None and row["plex_ok_at"]
    assert len(refreshes(fake)) == 1 and len(listings(fake)) == 1  # kun de senest tilføjede blev hentet
    later(clock, hours=3)
    r.run_once()
    assert len(listings(fake)) == 1  # bekræftet - tjekkes ikke igen


def test_missing_file_gets_a_new_scan_and_is_found_next_time(arr, tmp_path, caplog):  # noqa: F811
    video = movie(arr, 1)
    fake, r, db, clock = setup(arr, tmp_path)
    r.run_once()
    later(clock, minutes=2)
    with caplog.at_level(logging.INFO, logger="oversaetter"):
        r.run_once()  # Plex fandt intet første gang
        row = rows(db)["movie:1"]
        assert row["plex_state"] == "tjekkes" and row["plex_tries"] == 1 and len(refreshes(fake)) == 2
        later(clock, minutes=4)
        r.run_once()
        assert len(refreshes(fake)) == 2  # næste kontrol er først efter 5 minutter
        fake.files["1"].append(video)
        later(clock, minutes=1)
        r.run_once()
    row = rows(db)["movie:1"]
    assert row["plex_state"] == "ok" and row["plex_tries"] == 1 and len(refreshes(fake)) == 2
    assert "er ikke kommet i Plex - bad om en ny scanning (1. gang)" in caplog.text
    assert "er nu i Plex (efter 1 ekstra scanning(er))" in caplog.text
    assert "plex-token" not in caplog.text


def test_still_missing_after_the_fixed_checks_is_reported_then_found(arr, tmp_path):  # noqa: F811
    video = movie(arr, 1)
    fake, r, db, clock = setup(arr, tmp_path)
    r.run_once()
    for minutes, state in ((2, "tjekkes"), (5, "tjekkes"), (15, "tjekkes"), (60, "mangler")):
        later(clock, minutes=minutes)
        r.run_once()
        assert rows(db)["movie:1"]["plex_state"] == state
    assert rows(db)["movie:1"]["plex_tries"] == 4 and len(refreshes(fake)) == 5

    class R:
        revealer = r

    out = api.Api(r.cfg, R(), clock=lambda: clock["now"]).reveal({})
    assert [m["key"] for m in out["plex_missing"]] == ["movie:1"] and out["plex_fixed"] == []
    assert out["shown"][0]["plex"] == "mangler" and "path" not in out["plex_missing"][0]

    fake.files["1"].append(video)
    later(clock, minutes=60)
    r.run_once()
    assert rows(db)["movie:1"]["plex_state"] == "ok"
    out = api.Api(r.cfg, R(), clock=lambda: clock["now"]).reveal({})
    assert out["plex_missing"] == [] and [m["key"] for m in out["plex_fixed"]] == ["movie:1"]


def test_gives_up_after_a_day(arr, tmp_path, caplog):  # noqa: F811
    movie(arr, 1)
    fake, r, db, clock = setup(arr, tmp_path)
    r.run_once()
    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        for _ in range(40):
            later(clock, hours=1)
            r.run_once()
    row = rows(db)["movie:1"]
    assert row["plex_state"] == "opgivet" and row["plex_next"] is None
    assert "stadig ikke i Plex efter 24 timer" in caplog.text
    scans = len(refreshes(fake))
    later(clock, hours=5)
    r.run_once()
    assert len(refreshes(fake)) == scans  # ikke flere scanninger


def test_plex_not_answering_means_unknown_and_no_new_scan(arr, tmp_path, caplog):  # noqa: F811
    video = movie(arr, 1)
    fake, r, db, clock = setup(arr, tmp_path)
    r.run_once()
    fake.broken = True  # listen over filer kan ikke hentes
    later(clock, minutes=2)
    with caplog.at_level(logging.WARNING, logger="oversaetter"):
        r.run_once()
    row = rows(db)["movie:1"]
    assert row["plex_state"] == "tjekkes" and row["plex_tries"] == 0 and len(refreshes(fake)) == 1
    assert "kunne ikke tjekke, om nye filer er kommet i Plex" in caplog.text
    fake.broken = False
    fake.files["1"].append(video)
    later(clock, minutes=4)
    r.run_once()
    assert rows(db)["movie:1"]["plex_state"] == "tjekkes"  # prøver først igen efter 5 minutter
    later(clock, minutes=1)
    r.run_once()
    assert rows(db)["movie:1"]["plex_state"] == "ok"


def test_upgraded_file_on_an_old_title_is_found_in_the_whole_library(arr, tmp_path):  # noqa: F811
    video = episode(arr, 5)
    fake, r, db, clock = setup(arr, tmp_path, kinds={"1": "show"})
    fake.files["1"] = [video] + [f"/andet/afsnit{i}.mkv" for i in range(1500)]  # titlen er gammel i Plex
    r.run_once()
    later(clock, minutes=2)
    r.run_once()
    assert rows(db)["episode:5"]["plex_state"] == "ok" and len(refreshes(fake)) == 1
    calls = listings(fake)
    assert [c[1]["type"] for c in calls] == ["4", "4", "4"]  # afsnit: de nyeste + to sider af hele biblioteket
    assert calls[0][1]["sort"] == "addedAt:desc"
    assert [c[1]["X-Plex-Container-Start"] for c in calls[1:]] == ["0", "1000"]


def test_replaced_file_is_not_checked_and_folder_scanned_once_per_round(arr, tmp_path):  # noqa: F811
    first, second, gone = episode(arr, 5), episode(arr, 6), episode(arr, 7)
    fake, r, db, clock = setup(arr, tmp_path, kinds={"1": "show"})
    r.run_once()
    os.remove(gone)  # opgraderet: den nye fil har sin egen række
    later(clock, minutes=2)
    r.run_once()
    got = rows(db)
    assert got["episode:7"]["plex_state"] == "væk" and got["episode:7"]["plex_next"] is None
    assert got["episode:5"]["plex_tries"] == 1 and got["episode:6"]["plex_tries"] == 1
    assert os.path.dirname(first) == os.path.dirname(second) and len(refreshes(fake)) == 2  # én ny scanning af mappen


def test_can_be_switched_off(arr, tmp_path):  # noqa: F811
    movie(arr, 1)
    fake, r, db, clock = setup(arr, tmp_path, reveal_verify=False)
    r.run_once()
    later(clock, hours=2)
    r.run_once()
    assert rows(db)["movie:1"]["plex_state"] is None and listings(fake) == [] and len(refreshes(fake)) == 1


def test_library_lookup_reuses_answers(arr, tmp_path):  # noqa: F811
    fake = PlexWithFiles({"1": ["/m"]})
    fake.files["1"] = ["/m/A/a.mkv"]
    lookup = Library(Plex("http://plex", "t", fake.client()))
    assert lookup.has("/m/A/a.mkv") and not lookup.has("/m/B/b.mkv") and not lookup.has("/m/C/c.mkv")
    assert len(listings(fake)) == 2  # de nyeste én gang, hele biblioteket én gang
