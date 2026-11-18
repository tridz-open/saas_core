import importlib
from types import SimpleNamespace

from tests.test_webhooks import setup_gw, rz_req


def _ev(eid="e1", kind="payment.succeeded"):
    return SimpleNamespace(event_id=eid, kind=kind, subscription_ref="sub_1", tenant_ref=None, raw={"a": 1})


def test_record_event_once(frappe):
    wh = importlib.import_module("saas_core.webhooks")
    d1, new1 = wh.record_event("razorpay", _ev())
    d2, new2 = wh.record_event("razorpay", _ev())
    assert (new1, new2) == (True, False) and d1.name == d2.name == "razorpay:e1"
    assert len(frappe._store.enqueued) == 1


def test_duplicate_entry_race(frappe, monkeypatch):
    wh = importlib.import_module("saas_core.webhooks")
    wh.record_event("razorpay", _ev("r"))
    monkeypatch.setattr(frappe.db, "exists", lambda *a, **k: None)
    _, new = wh.record_event("razorpay", _ev("r"))
    assert new is False and len(frappe._store.enqueued) == 1


def test_ingest_twice_one_event_one_job(frappe):
    setup_gw(frappe)
    wh = importlib.import_module("saas_core.webhooks")
    h, b = rz_req(eid="evt_9")
    assert wh.ingest("razorpay", h, b)["status"] == "accepted"
    assert wh.ingest("razorpay", h, b)["status"] == "duplicate"
    assert len([k for k in frappe._store.docs if k[0] == "Payment Event"]) == 1
    assert len(frappe._store.enqueued) == 1
