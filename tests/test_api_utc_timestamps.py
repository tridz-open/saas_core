import datetime as dt



def test_iso_utc_fields_get_z(frappe):
    from saas_core.api.v1.serializers import iso
    assert iso(dt.datetime(2026, 11, 30, 20, 30), utc=True) == "2026-11-30T20:30:00Z"
    assert iso(None) is None


def test_iso_site_local_converted_to_utc(frappe):
    from saas_core.api.v1.serializers import iso
    # stub site clock is IST (UTC+5:30)
    assert iso(dt.datetime(2026, 12, 1, 2, 0)) == "2026-11-30T20:30:00Z"
    assert iso("2026-12-01 02:00:00") == "2026-11-30T20:30:00Z"


def test_invoice_and_subscription_emit_z(frappe):
    from saas_core.api.v1.serializers import to_invoice, to_subscription
    inv = to_invoice({"name": "I", "issued_at": dt.datetime(2026, 11, 30, 20, 30), "paid_at": None,
                      "period_start": dt.datetime(2026, 11, 30, 18, 30), "period_end": dt.datetime(2026, 12, 31, 18, 30)})
    assert inv["issued_at"] == "2026-11-30T20:30:00Z" and inv["period_end"].endswith("Z")
    assert to_subscription({"period_end": dt.datetime(2026, 12, 31, 18, 30)})["period_end"] == "2026-12-31T18:30:00Z"
