def stamps(*days):
    return [f"{d}_020000" for d in days]


def test_select_prune_keeps_daily_and_weekly():
    from saas_core.provisioning.backups import select_prune
    all_ = stamps(*[f"202601{d:02d}" for d in range(1, 29)])
    gone = select_prune(all_, 7, 4)
    kept = set(all_) - set(gone)
    assert set(all_[-7:]) <= kept            # last 7 days
    assert len(kept) < 28 and "20260128_020000" in kept
    assert select_prune(all_, 100, 100) == []


def test_run_daily_backups_prunes(frappe):
    from datetime import datetime
    from saas_core.provisioning import backups
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    frappe.conf["saas_core_offsite_backups"] = 1; frappe.conf["saas_core_backup_bucket"] = "acme-backups"
    frappe.conf["saas_core_backup_keep_daily"] = 2
    frappe.conf["saas_core_backup_keep_weekly"] = 1
    frappe.get_doc({"doctype": "Tenant", "name": "acme", "slug": "acme", "status": "active", "fc_site": "acme.apps.test"}).insert()
    frappe.get_doc({"doctype": "Tenant", "name": "gone", "slug": "gone", "status": "suspended", "fc_site": "gone.apps.test"}).insert()
    port = FakeFcctl(); port.sites["acme.apps.test"] = {}
    for d in range(1, 6):
        port.bucket_put("acme-backups", f"tenants/acme/2026010{d}_020000/database.sql.gz", b"x")
    out = backups.run_daily_backups(port, now=datetime(2026, 1, 6, 2, 0, 0))
    assert out["ok"] == ["acme"] and not out["failed"]
    assert port.count("create_backup") == 1
    left = sorted({k.split("/")[2] for k in port.bucket_list("acme-backups", "tenants/acme/")})
    assert left == ["20260105_020000", "20260106_020000"]


def test_failure_isolated(frappe):
    from saas_core.provisioning import backups
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    frappe.get_doc({"doctype": "Tenant", "name": "a", "slug": "a", "status": "active", "fc_site": "a.t"}).insert()
    out = backups.run_daily_backups(FakeFcctl(fail_at_step="create_backup"))
    assert "a" in out["failed"] and out["ok"] == []


class _NoBucketPort:
    """Like the real FcctlAdapter: create_backup only."""
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def create_backup(self, site, *, workflow_id=None):
        from types import SimpleNamespace as NS
        self.calls.append(site)
        return NS(ok=not self.fail, message="boom" if self.fail else "", handle="h1")


def _tenant(frappe, n="a"):
    return frappe.get_doc({"doctype": "Tenant", "name": n, "slug": n, "status": "active", "fc_site": f"{n}.t"}).insert()


def test_no_bucket_methods_backs_up_without_status_change(frappe):
    from saas_core.provisioning import backups
    _tenant(frappe)
    port = _NoBucketPort()
    out = backups.run_daily_backups(port)
    assert port.calls == ["a.t"] and out["ok"] == ["a"] and not out["failed"] and out["offsite"] is False
    assert frappe.db.get_value("Tenant", "a", "status") == "active"


def test_offsite_on_but_methods_missing_skips_with_warning(frappe, caplog):
    import logging
    from saas_core.provisioning import backups
    frappe.conf["saas_core_offsite_backups"] = 1; frappe.conf["saas_core_backup_bucket"] = "acme-backups"
    _tenant(frappe)
    port = _NoBucketPort()
    with caplog.at_level(logging.WARNING):
        out = backups.run_daily_backups(port)
    assert "offsite backups not configured" in caplog.text
    assert out["offsite"] is False and out["ok"] == ["a"] and port.calls == ["a.t"]


def test_backup_failure_keeps_tenant_active_and_audits(frappe):
    from saas_core.provisioning import backups
    _tenant(frappe)
    out = backups.run_daily_backups(_NoBucketPort(fail=True))
    assert "a" in out["failed"]
    assert frappe.db.get_value("Tenant", "a", "status") == "active"
    assert frappe.get_all("Audit Event", filters={"action": "backup_failed", "target": "a"})
