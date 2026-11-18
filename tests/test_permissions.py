"""Permission query + access assertion tests."""
import pytest


def _as(frappe, user, roles):
    frappe.session.user = user
    frappe._roles = roles
    frappe.db.escape = lambda v: "'" + str(v).replace("'", "''") + "'"


def test_admins_see_all(frappe):
    import importlib; p = importlib.import_module("saas_core.permissions")
    for roles in (["SaaS Admin"], ["System Manager"]):
        _as(frappe, "x@x.com", roles)
        assert p.tenant_query() == "" and p.ticket_query() == "" and p.job_query("x@x.com") == ""
    _as(frappe, "Administrator", [])
    assert p.invoice_query() == ""


def test_customer_queries(frappe):
    import importlib; p = importlib.import_module("saas_core.permissions")
    _as(frappe, "c@x.com", ["SaaS Customer"])
    assert p.tenant_query() == "`tabTenant`.`owner_user` = 'c@x.com'"
    assert p.ticket_query() == "`tabSupport Ticket`.`owner_user` = 'c@x.com'"
    for q in (p.subscription_query(), p.invoice_query(), p.job_query()):
        assert "from `tabTenant`" in q and "'c@x.com'" in q


def test_escaping(frappe):
    import importlib; p = importlib.import_module("saas_core.permissions")
    _as(frappe, "a'b", ["SaaS Customer"])
    assert "'a''b'" in p.tenant_query()


def test_has_permission(frappe):
    import importlib; p = importlib.import_module("saas_core.permissions")
    _as(frappe, "c@x.com", ["SaaS Customer"])
    frappe._store.docs[("Tenant", "t1")] = frappe._dict(doctype="Tenant", name="t1", owner_user="c@x.com")
    assert p.has_tenant_permission(frappe._dict(owner_user="c@x.com"), "read")
    assert not p.has_tenant_permission(frappe._dict(owner_user="o@x.com"), "read")
    assert p.has_tenant_child_permission(frappe._dict(tenant="t1"))
    assert not p.has_tenant_child_permission(frappe._dict(tenant="nope"))
    assert not p.has_tenant_child_permission(frappe._dict())
    assert p.has_ticket_permission(frappe._dict(owner_user="c@x.com"))
    assert not p.has_ticket_permission(frappe._dict(owner_user="o"))


def test_assert_tenant_access(frappe):
    import importlib; p = importlib.import_module("saas_core.permissions")
    frappe._store.docs[("Tenant", "t1")] = frappe._dict(doctype="Tenant", name="t1", owner_user="c@x.com")
    _as(frappe, "c@x.com", ["SaaS Customer"])
    assert p.assert_tenant_access("t1").name == "t1"
    _as(frappe, "other@x.com", ["SaaS Customer"])
    with pytest.raises(frappe.DoesNotExistError):
        p.assert_tenant_access("t1")
    with pytest.raises(frappe.DoesNotExistError):
        p.assert_tenant_access("missing")
    _as(frappe, "boss@x.com", ["SaaS Admin"])
    assert p.assert_tenant_access("t1").name == "t1"
