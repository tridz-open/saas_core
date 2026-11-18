"""Tenant backup ops: tenants.backups.list/create/download_link (via FcctlPort, never exposing FC internals)."""
from __future__ import annotations

import base64
from datetime import timedelta

import frappe

from saas_core.api.v1._common import authenticated, ApiError, rate_limit, require_login, require_experimental
from saas_core.api.v1.tenants import access, run_idempotent, safe_job, _iso

PARTS = ("database", "public", "private")


def _g(o, k, d=None):
    return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)


def _backup(o) -> dict:
    return {
        "name": str(_g(o, "name")),
        "created_at": _iso(_g(o, "created_at")),
        "size_bytes": _g(o, "size_bytes"),
        "kind": _g(o, "kind") if _g(o, "kind") in ("scheduled", "on_demand") else "scheduled",
        "parts": [p for p in (_g(o, "parts") or PARTS) if p in PARTS],
    }


def _offset(cursor):
    try:
        return max(int(base64.urlsafe_b64decode(cursor.encode()).decode()), 0) if cursor else 0
    except Exception:
        raise ApiError("validation_error", "Invalid cursor", 400, {"cursor": "invalid"})


@authenticated()
def list(cursor=None, limit=None, tenant=None):
    user = require_login()
    rate_limit(f"backups.list:{user}", 60, 60)
    doc = access(tenant, user)
    try:
        n = min(max(int(limit or 20), 1), 100)
    except (TypeError, ValueError):
        raise ApiError("validation_error", "Invalid limit", 400, {"limit": "invalid"})
    off = _offset(cursor)
    from saas_core.provisioning import runner
    try:
        rows = [_backup(b) for b in runner.get_port().list_backups(doc.fc_site)]
    except Exception:
        frappe.log_error("list_backups failed")
        raise ApiError("gateway_error", "Could not load backups, try again shortly", 502)
    rows.sort(key=lambda b: str(b["created_at"]), reverse=True)
    page = rows[off: off + n]
    nxt = base64.urlsafe_b64encode(str(off + n).encode()).decode() if off + n < len(rows) else None
    return {"items": page, "next_cursor": nxt}


@authenticated(methods=["POST"])
def create(tenant=None):
    user = require_login()
    rate_limit(f"backups.create:{user}", 10, 60)
    doc = access(tenant, user, "write")
    if doc.status not in ("active", "suspended"):
        raise ApiError("conflict", "Tenant is not ready for backups", 409)

    def do():
        from saas_core import jobs, audit
        job = frappe.get_doc("Provision Job", jobs.enqueue_job(
            doc.name, "backup", args={"kind": "on_demand"}, dedupe_key=f"backup:{doc.name}"))
        audit.log("tenant.backup", actor=user, target_doctype="Tenant", target=doc.name)
        return safe_job(job)

    return run_idempotent("backups.create", user, {"tenant": tenant}, do, 202)


@authenticated(methods=["POST"])
def download_link(tenant=None, backup=None, part=None):
    require_experimental("restore")
    user = require_login()
    rate_limit(f"backups.download_link:{user}", 20, 60)
    doc = access(tenant, user)
    if part not in PARTS:
        raise ApiError("validation_error", "Invalid part", 400, {"part": "must be database, public or private"})
    if not backup or not isinstance(backup, str):
        raise ApiError("validation_error", "backup is required", 400, {"backup": "required"})
    from saas_core.provisioning import runner
    from saas_core import audit
    port = runner.get_port()
    try:
        if backup not in {str(_g(b, "name")) for b in port.list_backups(doc.fc_site)}:
            raise ApiError("not_found", "Backup not found", 404)
        res = port.backup_download_link(doc.fc_site, backup)
    except ApiError:
        raise
    except Exception:
        frappe.log_error("backup_download_link failed")
        raise ApiError("gateway_error", "Could not create download link, try again shortly", 502)
    url = res if isinstance(res, str) else _g(res, "url")
    exp = frappe.utils.now_datetime() + timedelta(minutes=10)
    audit.log("tenant.backup_download", actor=user, target_doctype="Tenant", target=doc.name, details={"backup": backup, "part": part})
    return {"url": str(url), "expires_at": _iso(exp)}


@authenticated(methods=["POST"])
def restore(tenant=None, backup=None):
    """Guarded restore: owner-only; creates a pending Approval Request (no job until approved)."""
    require_experimental("restore")
    import hashlib
    import json
    user = require_login()
    rate_limit(f"backups.restore:{user}", 5, 60)
    doc = access(tenant, user, "write")
    if not backup or not isinstance(backup, str):
        raise ApiError("validation_error", "backup is required", 400, {"backup": "required"})
    if doc.status not in ("active", "suspended"):
        raise ApiError("conflict", "Tenant is not ready for restore", 409)

    def do():
        from saas_core.provisioning import runner
        from saas_core import audit
        try:
            found = [b for b in runner.get_port().list_backups(doc.fc_site) if str(_g(b, "name")) == backup]
        except Exception:
            frappe.log_error("list_backups failed")
            raise ApiError("gateway_error", "Could not load backups, try again shortly", 502)
        if not found:
            raise ApiError("not_found", "Backup not found", 404)
        args = {"backup": backup, "restore": True}
        digest = hashlib.sha256(json.dumps(["restore_backup", doc.name, args], sort_keys=True).encode()).hexdigest()
        blast = {"tenant": doc.name, "overwrites": "all current data", "backup": backup,
                 "backup_created_at": _iso(_g(found[0], "created_at")), "reversible": False}
        apr = frappe.get_doc({"doctype": "Approval Request", "op": "restore_backup", "target": doc.name,
                              "args": json.dumps(args), "digest": digest, "requested_by": user,
                              "approved_by": "[]", "status": "pending", "blast_radius": json.dumps(blast)})
        apr.insert(ignore_permissions=True)
        audit.log("tenant.backup_restore_requested", actor=user, target_doctype="Tenant", target=doc.name,
                  details={"approval": apr.name, "backup": backup})
        return {"approval_request": apr.name, "status": "pending_approval"}

    return run_idempotent("backups.restore", user, {"tenant": tenant, "backup": backup}, do, 202)
