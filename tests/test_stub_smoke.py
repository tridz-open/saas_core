def test_stub(frappe):
    d = frappe.new_doc("Tenant", name="t1", status="active").insert()
    assert frappe.get_all("Tenant", {"status": "active"}, pluck="name") == ["t1"]
    assert frappe.db.exists("Tenant", "t1")
