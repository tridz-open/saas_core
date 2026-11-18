def test_chain_ok_and_tamper(frappe):
    import importlib; audit = importlib.import_module("saas_core.audit")
    n1 = audit.log("a.one", actor="u", details={"k": 1})
    audit.log("a.two", target_doctype="Tenant", target="t")
    audit.log("a.three")
    assert audit.verify_chain() == {"ok": True, "checked": 3, "broken_at": None}
    docs = {d["seq"]: d for (dt, _), d in frappe._store.docs.items() if dt == "Audit Event"}
    assert docs[2]["prev_hash"] == docs[1]["hash"] and docs[1]["prev_hash"] == "0" * 64
    docs[2]["action"] = "evil"
    assert audit.verify_chain()["broken_at"] == 2
    docs[2]["action"] = "a.two"
    assert audit.verify_chain()["ok"]
    del frappe._store.docs[("Audit Event", docs[2]["name"])]
    assert audit.verify_chain() == {"ok": False, "checked": 1, "broken_at": 3}
    assert n1


def test_empty_chain(frappe):
    import importlib; audit = importlib.import_module("saas_core.audit")
    assert audit.verify_chain()["ok"]
