"""Spool inventory: matching to toolheads, deducting finished prints, cost."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.printers.base import Status, Toolhead
from app.spools import AssignIn, SettingsIn, SpoolIn, Spools, WeighIn
from app.store import Store

from test_library import library  # noqa: F401  pytest fixture


class FakePrinter:
    def __init__(self, heads):
        self.status = Status(online=True, state="idle", toolheads=heads)


class FakeHub:
    def __init__(self, heads):
        self.printers = {"u1": FakePrinter(heads)}

    def get(self, pid):
        return self.printers.get(pid)


def heads():
    return [
        Toolhead(0, "PLA", "#FF0000", "Snapmaker", True),
        Toolhead(1, "PETG", "#00FF00", "Generic", True),
        Toolhead(2, "PLA", "#0000FF", "Polymaker", True),
        Toolhead(3, "", "", "", False),
    ]


@pytest.fixture()
def sp(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    return Spools(tmp_path / "db.sqlite", store, FakeHub(heads()))


def add(sp, **kw):
    return sp.create(SpoolIn(**{"total_g": 1000, "price": 20.0, **kw}))


def test_auto_match_by_material_colour_brand(sp):
    red = add(sp, vendor="Snapmaker", material="PLA", color="#FE0101")
    green = add(sp, vendor="eSun", material="PETG", color="#00FF00")  # brand differs from "Generic"? both sides known
    blue = add(sp, material="PLA", color="#0000FF")
    assert sp.get(red["id"])["printer"] == "u1" and sp.get(red["id"])["toolhead"] == 0
    assert sp.get(green["id"])["printer"] is None  # eSun vs Generic: not the same spool
    assert sp.get(blue["id"])["toolhead"] == 2


def test_swap_returns_spool_to_shelf(sp):
    red = add(sp, vendor="Snapmaker", material="PLA", color="#FF0000")
    assert sp.get(red["id"])["toolhead"] == 0
    sp.hub.printers["u1"].status.toolheads[0] = Toolhead(0, "PETG", "#FFFFFF", "Snapmaker", True)
    sp.sync()
    assert sp.get(red["id"])["printer"] is None


def test_manual_assign_and_unassign_sticks(sp):
    s = add(sp, material="ABS", color="#123456")
    assert sp.get(s["id"])["printer"] is None  # nothing loaded matches
    sp.assign(s["id"], AssignIn(printer="u1", toolhead=1))
    sp.sync()
    assert sp.get(s["id"])["toolhead"] == 1  # hand placement survives a sync
    red = add(sp, vendor="Snapmaker", material="PLA", color="#FF0000")
    sp.assign(red["id"], AssignIn(printer=None))
    sp.sync()
    assert sp.get(red["id"])["printer"] is None  # not put straight back


def test_charge_job_deducts_once_and_costs(sp):
    red = add(sp, vendor="Snapmaker", material="PLA", color="#FF0000", price=25.0)
    sp.update_settings(SettingsIn(kwh_price=0.30, watts={"u1": 200}))
    res = sp.charge_job("u1:1", "u1", [{"toolhead": 0, "grams": 40}], seconds=3600)
    assert res["grams"] == 40
    assert res["filament_cost"] == 1.0  # 40 g at $25/kg
    assert res["power_cost"] == 0.06  # 0.2 kW x 1 h x $0.30
    assert res["by_tool"][0]["spool_id"] == red["id"]
    assert sp.get(red["id"])["remaining_g"] == 960
    again = sp.charge_job("u1:1", "u1", [{"toolhead": 0, "grams": 40}], seconds=3600)
    assert again == res and sp.get(red["id"])["remaining_g"] == 960


def test_on_print_finished_prorates_cancelled_and_skips_backfill(sp):
    red = add(sp, vendor="Snapmaker", material="PLA", color="#FF0000")
    written = {}
    history = SimpleNamespace(set_usage=lambda pid, **kw: written.update({pid: kw}))
    rec = {"id": 7, "printer": "u1", "job_key": "u1:000007", "status": "cancelled", "progress": 0.25,
           "duration": 600, "filament": [{"toolhead": 0, "slot": 0, "type": "PLA", "grams": 80.0}]}
    res = sp.on_print_finished(rec, history)
    assert res["grams"] == 20
    assert sp.get(red["id"])["remaining_g"] == 980
    assert written[7]["grams"] == 20 and written[7]["filament"][0]["spool_id"] == red["id"]
    old = {**rec, "id": 8, "job_key": "u1:000008", "status": "success", "backfill": True}
    res = sp.on_print_finished(old, history)
    assert res["grams"] == 80 and res["cost"] > 0
    assert sp.get(red["id"])["remaining_g"] == 980  # old jobs are costed, not deducted


def test_weigh_in_subtracts_empty_spool(sp):
    s = add(sp, material="PLA", color="#FFFFFF", empty_g=180)
    assert sp.weigh(s["id"], WeighIn(gross_g=680))["remaining_g"] == 500


def test_print_dialog_warns_when_spool_is_short(sp):
    red = add(sp, vendor="Snapmaker", material="PLA", color="#FF0000", remaining_g=30)
    rows = [{"index": 0, "file_index": 0, "file": {"type": "PLA", "grams": 45.0}, "ok": True}]
    warnings = sp.check_tools("u1", rows)
    assert warnings and "30 g left" in warnings[0]
    assert rows[0]["ok"] is False and rows[0]["spool"]["id"] == red["id"]


def test_api_roundtrip(tmp_path, library):  # noqa: F811
    settings = Settings(library=library, data=tmp_path / "data", password="", scan_interval=0)
    app = create_app(settings, background_scan=False)
    assert len(app.state.history._listeners) == 1  # finished prints reach the spools
    with TestClient(app) as c:
        r = c.post("/api/spools", json={"material": "pla", "color": "#ff0000", "total_g": 1000, "price": 18})
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["material"] == "PLA" and s["color"] == "#FF0000" and s["price_per_kg"] == 18
        assert c.post(f"/api/spools/{s['id']}/weigh", json={"remaining_g": 400}).json()["remaining_g"] == 400
        assert c.put("/api/spools/settings", json={"kwh_price": 0.25}).json()["kwh_price"] == 0.25
        assert c.get("/api/spools/pricing").json()["materials"]["PLA"] == 18
        assert len(c.get("/api/spools").json()["spools"]) == 1
        assert c.post("/api/spools", json={"color": "red"}).status_code == 400
        assert c.delete(f"/api/spools/{s['id']}").json()["ok"]
        assert c.get(f"/api/spools/{s['id']}").status_code == 404
