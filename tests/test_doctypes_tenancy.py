"""Tenancy DocType JSON + controller rule tests."""
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).parent.parent / "saas_core" / "saas_core" / "doctype"

EXPECTED = {
    "tenant": ["slug", "owner_user", "status", "plan", "template", "region", "fc_server_id", "fc_site", "custom_domain",
               "created_via", "suspend_reason", "active_job", "workflow_id", "recipe_fields",
               "admin_password", "setup_secret", "setup_email_sent_at", "setup_completed_at", "welcome_sent_at"],
    "template": ["slug", "title", "description", "is_public", "min_plan", "recipe", "apps", "backup_source", "hooks",
                 "sanitize_config", "health_checks", "site_config", "site_admin"],
    "template_app": ["app", "source_repo", "branch", "version", "install_order"],
    "provision_job": ["tenant", "kind", "state", "workflow_id", "attempts", "needs_attention", "error", "args", "approval",
                      "dedupe_key", "steps", "finished_at"],
    "provision_job_step": ["seq", "step_name", "state", "hook_version", "started_at", "finished_at", "message", "handle"],
    "support_ticket": ["tenant", "owner_user", "subject", "body", "status", "priority", "replies"],
    "support_ticket_reply": ["from_staff", "body", "at", "author"],
    "audit_event": ["seq", "at", "actor", "action", "target_doctype", "target", "details", "prev_hash", "hash"],
    "approval_request": ["op", "target", "args", "digest", "requested_by", "approved_by", "status", "expires_at",
                         "journal_id", "blast_radius"],
    "platform_session": ["login", "blob", "version"],
}


@pytest.mark.parametrize("snake", sorted(EXPECTED))
def test_json_fields_and_files(snake):
    d = ROOT / snake
    assert (d / "__init__.py").exists() and (d / f"{snake}.py").exists()
    j = json.loads((d / f"{snake}.json").read_text())
    assert j["module"] == "Saas Core"
    assert [f["fieldname"] for f in j["fields"]] == EXPECTED[snake]
    assert j["field_order"] == EXPECTED[snake]


def test_enums_and_naming():
    def load(s):
        return json.loads((ROOT / s / f"{s}.json").read_text())
    f = {x["fieldname"]: x for x in load("tenant")["fields"]}
    assert f["status"]["options"].split("\n") == ["pending", "provisioning", "active", "suspended", "terminating", "failed"]
    assert load("tenant")["autoname"] == "field:slug"
    assert load("provision_job")["autoname"] == "PJ-.######"
    assert load("audit_event")["autoname"] == "AUD-.#########"
    assert load("platform_session")["autoname"] == "field:login"
    assert load("provision_job_step")["istable"] == 1


def test_permissions_json():
    def roles(s):
        return {p["role"]: p for p in json.loads((ROOT / s / f"{s}.json").read_text())["permissions"]}
    assert roles("tenant")["SaaS Admin"]["write"] == 1 and roles("tenant")["System Manager"]["delete"] == 1
    assert set(roles("platform_session")) == {"System Manager", "SaaS Platform Worker"}
    assert not any(p.get("write") or p.get("delete") for p in roles("audit_event").values())


def _tenant(frappe, slug="acme-shop", status="pending", old=None):
    from saas_core.saas_core.doctype.tenant.tenant import Tenant
    t = Tenant(doctype="Tenant", slug=slug, owner_user="a@x.com", status=status, name=slug)
    if old:
        t.get_doc_before_save = lambda: frappe._dict(status=old)
    return t


@pytest.mark.parametrize("slug", ["ab", "1abc", "Abc-d", "abc-", "a" * 33, "ab_c", "www", "admin"])
def test_bad_slugs(frappe, slug):
    with pytest.raises(frappe.ValidationError):
        _tenant(frappe, slug).validate()


def test_good_slug(frappe):
    _tenant(frappe, "my-shop-1").validate()


@pytest.mark.parametrize("old,new,ok", [
    ("pending", "provisioning", True), ("provisioning", "active", True), ("active", "suspended", True),
    ("suspended", "active", True), ("active", "terminating", True), ("provisioning", "failed", True),
    ("pending", "active", False), ("terminating", "active", False), ("active", "pending", False),
])
def test_status_transitions(frappe, old, new, ok):
    t = _tenant(frappe, status=new, old=old)
    if ok:
        t.validate()
    else:
        with pytest.raises(frappe.ValidationError):
            t.validate()


def test_new_tenant_must_be_pending(frappe):
    with pytest.raises(frappe.ValidationError):
        _tenant(frappe, status="active").validate()


def test_job_terminal_immutable(frappe):
    from saas_core.saas_core.doctype.provision_job.provision_job import ProvisionJob
    mk = lambda **k: ProvisionJob(doctype="Provision Job", name="PJ-1", tenant="t", kind="provision", **k)
    j = mk(state="running"); j.validate()
    with pytest.raises(frappe.ValidationError):
        mk(state="bogus").validate()
    done = mk(state="failed", needs_attention=0)
    done.get_doc_before_save = lambda: frappe._dict(state="failed", kind="provision", tenant="t", needs_attention=0)
    done.validate()
    done["needs_attention"] = 1
    done.validate()  # flag allowed
    done["state"] = "running"
    with pytest.raises(frappe.ValidationError):
        done.validate()


def test_audit_immutable(frappe):
    from saas_core.saas_core.doctype.audit_event.audit_event import AuditEvent
    a = AuditEvent(doctype="Audit Event", name="AUD-1")
    a.flags = {"in_insert": True}
    a.on_update()
    a.flags = {}
    with pytest.raises(frappe.ValidationError):
        a.on_update()
    with pytest.raises(frappe.ValidationError):
        a.on_trash()


def test_approval_rules(frappe):
    from saas_core.saas_core.doctype.approval_request.approval_request import ApprovalRequest
    mk = lambda **k: ApprovalRequest(doctype="Approval Request", name="APR-1", requested_by="r@x.com", **k)
    mk(status="pending", approved_by=[]).validate()
    with pytest.raises(frappe.ValidationError):
        mk(status="pending", approved_by=["r@x.com"]).validate()
    with pytest.raises(frappe.ValidationError):
        mk(status="approved", approved_by=["a@x.com"]).validate()
    mk(status="approved", approved_by=["a@x.com", "b@x.com"]).validate()
    bad = mk(status="executed", approved_by=["a@x.com", "b@x.com"])
    bad.get_doc_before_save = lambda: frappe._dict(status="pending")
    with pytest.raises(frappe.ValidationError):
        bad.validate()


def test_install_idempotent(frappe):
    import importlib; install = importlib.import_module("saas_core.install")
    install.after_install()
    n = len(frappe._store.docs)
    install.after_install()
    assert len(frappe._store.docs) == n
    assert {k[1] for k in frappe._store.docs if k[0] == "Role"} == {"SaaS Admin", "SaaS Customer", "SaaS Platform Worker"}
