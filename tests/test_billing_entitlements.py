def mk(frappe, dt_, **kw):
    return frappe.get_doc({"doctype": dt_, **kw}).insert()


def test_can_create(frappe):
    from saas_core.billing import entitlements as e
    assert e.can_create_tenant("u", "nope") == (False, "plan_not_found")
    mk(frappe, "Plan", name="p", available=0)
    assert e.can_create_tenant("u", "p")[1] == "plan_unavailable"
    frappe.db.set_value("Plan", "p", "available", 1)
    assert e.can_create_tenant("u", "p") == (True, "")
    frappe.conf["saas_core_capacity_open"] = False
    assert e.can_create_tenant("u", "p") == (False, "capacity_closed")
    frappe.conf["saas_core_capacity_open"] = True
    frappe.conf["saas_core_max_tenants_per_user"] = 1
    mk(frappe, "Tenant", name="t", owner_user="u", status="active")
    assert e.can_create_tenant("u", "p")[1] == "tenant_limit_reached"
    assert e.can_create_tenant("other", "p")[0]


def test_can_change_plan(frappe):
    from saas_core.billing import entitlements as e
    mk(frappe, "Plan", name="a", available=1, is_public=1, sort_order=1)
    mk(frappe, "Plan", name="b", available=1, is_public=1, sort_order=2)
    mk(frappe, "Plan", name="hid", available=1, is_public=0)
    mk(frappe, "Tenant", name="tc", owner_user="u", status="active", plan="a")
    assert e.can_change_plan("u", "tc", "nope") == (False, "plan_not_found")
    assert e.can_change_plan("u", "tc", "hid") == (False, "plan_unavailable")
    assert e.can_change_plan("u", "tc", "a") == (False, "same_plan")
    assert e.can_change_plan("x", "tc", "b") == (False, "not_owner")
    assert e.can_change_plan("u", "tc", "b") == (True, "")
    frappe.conf["saas_core_max_tenants_per_user"] = 1  # cap must not matter
    assert e.can_change_plan("u", "tc", "b") == (True, "")
    frappe.db.set_value("Tenant", "tc", "status", "suspended")
    assert e.can_change_plan("u", "tc", "b") == (False, "tenant_not_active")
    assert e.plan_change_kind("a", "b") == "upgrade" and e.plan_change_kind("b", "a") == "downgrade"
