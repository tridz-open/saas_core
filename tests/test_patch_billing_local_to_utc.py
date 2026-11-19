import datetime as dt


def test_patch_converts_local_to_utc(frappe):
    st = frappe._store
    st.docs[("Invoice", "I1")] = frappe._Doc(doctype="Invoice", name="I1", issued_at=dt.datetime(2026, 12, 1, 2, 0),
                                             paid_at=None, period_end=dt.datetime(2026, 12, 31, 18, 30))
    st.docs[("Payment Event", "E1")] = frappe._Doc(doctype="Payment Event", name="E1", next_retry_at=dt.datetime(2026, 12, 1, 2, 0))
    st.docs[("Subscription", "S1")] = frappe._Doc(doctype="Subscription", name="S1", first_failure_at=dt.datetime(2026, 12, 1, 2, 0),
                                                  period_end=dt.datetime(2026, 12, 31, 18, 30))
    frappe.db.set_value = _wrap(frappe.db.set_value)
    from saas_core.patches import billing_local_to_utc as p
    p.execute()
    assert st.docs[("Invoice", "I1")]["issued_at"] == dt.datetime(2026, 11, 30, 20, 30)
    assert st.docs[("Invoice", "I1")]["paid_at"] is None
    assert st.docs[("Invoice", "I1")]["period_end"] == dt.datetime(2026, 12, 31, 18, 30)  # untouched
    assert st.docs[("Payment Event", "E1")]["next_retry_at"] == dt.datetime(2026, 11, 30, 20, 30)
    assert st.docs[("Subscription", "S1")]["first_failure_at"] == dt.datetime(2026, 11, 30, 20, 30)
    assert st.docs[("Subscription", "S1")]["period_end"] == dt.datetime(2026, 12, 31, 18, 30)


def test_patch_noop_when_utc(frappe):
    frappe.utils.get_system_timezone = lambda: "UTC"
    frappe._store.docs[("Invoice", "I1")] = frappe._Doc(doctype="Invoice", name="I1", issued_at=dt.datetime(2026, 12, 1, 2, 0))
    from saas_core.patches import billing_local_to_utc as p
    p.execute()
    assert frappe._store.docs[("Invoice", "I1")]["issued_at"] == dt.datetime(2026, 12, 1, 2, 0)


def _wrap(orig):
    def sv(dt_, name, field, value=None, *a, **k):
        return orig(dt_, name, field, value)
    return sv
