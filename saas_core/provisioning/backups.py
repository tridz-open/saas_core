"""C6: bucket-backed template restore, daily tenant backups, retention. Talks only to the FcctlPort."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

TS_RE = re.compile(r"(\d{8}_\d{6})")
DEFAULT_KEEP_DAILY, DEFAULT_KEEP_WEEKLY = 7, 4


class BackupError(Exception):
    pass


def bucket_parts(source: dict) -> list[dict]:
    """Normalise a bucket backup_source to [{name,key,sha256}]. source: {bucket, prefix|key, manifest:{parts:[...]}}."""
    prefix = (source.get("prefix") or source.get("key") or "").rstrip("/")
    parts = (source.get("manifest") or {}).get("parts") or []
    if not source.get("bucket") or not parts:
        raise BackupError("bucket source needs bucket and manifest.parts")
    out = []
    for p in parts:
        if not p.get("name") or not p.get("sha256"):
            raise BackupError("each manifest part needs name and sha256")
        out.append({"name": p["name"], "key": p.get("key") or f"{prefix}/{p['name']}", "sha256": p["sha256"]})
    return out


def restore_bucket_source(port, site: str, source: dict, *, workflow_id: str | None = None):
    """Download parts, verify sha256, POST /uploads, PUT parts, restore {parts, upload_id}."""
    if isinstance(source.get("manifest"), str):  # manifest stored as a bucket path: fetch it first
        import json as _json
        raw = port.bucket_get(source["bucket"], source["manifest"])
        try:
            source = {**source, "manifest": _json.loads(raw.decode() if isinstance(raw, bytes) else raw)}
        except ValueError as e:
            raise BackupError(f"manifest {source['manifest']} is not valid JSON: {e}")
    parts, blobs = bucket_parts(source), {}
    for p in parts:
        data = port.bucket_get(source["bucket"], p["key"])
        if hashlib.sha256(data).hexdigest() != p["sha256"]:
            raise BackupError(f"sha256 mismatch for {p['name']}")
        blobs[p["name"]] = data
    upload_id = port.create_upload(site)
    for name, data in blobs.items():
        port.put_upload_part(upload_id, name, data)
    return port.restore_from_upload(site, {"parts": [p["name"] for p in parts], "upload_id": upload_id},
                                    workflow_id=workflow_id)


# -- retention
def _ts(s: str) -> datetime | None:
    m = TS_RE.search(s)
    return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") if m else None


def select_prune(stamps: list[str], keep_daily: int = DEFAULT_KEEP_DAILY, keep_weekly: int = DEFAULT_KEEP_WEEKLY) -> list[str]:
    """Return stamps to delete. Keep the newest backup of each of the last `keep_daily` days and the newest of
    each of the last `keep_weekly` ISO weeks (weeks counted from those present). Unparseable stamps are kept."""
    dated = sorted(((_ts(s), s) for s in stamps if _ts(s)), reverse=True)
    keep, days, weeks = set(), [], []
    for d, s in dated:
        day, wk = d.date(), d.isocalendar()[:2]
        if day not in days:
            days.append(day)
            if len(days) <= keep_daily:
                keep.add(s)
        if wk not in weeks:
            weeks.append(wk)
            if len(weeks) <= keep_weekly:
                keep.add(s)
    return [s for _, s in dated if s not in keep]


def prune_tenant_backups(port, slug: str, bucket: str, keep_daily=DEFAULT_KEEP_DAILY,
                         keep_weekly=DEFAULT_KEEP_WEEKLY) -> list[str]:
    base = f"tenants/{slug}/"
    keys = port.bucket_list(bucket, base)
    by_stamp: dict[str, list[str]] = {}
    for k in keys:
        m = TS_RE.search(k[len(base):])
        if m:
            by_stamp.setdefault(m.group(1), []).append(k)
    gone = select_prune(list(by_stamp), keep_daily, keep_weekly)
    for s in gone:
        for k in by_stamp[s]:
            port.bucket_delete(bucket, k)
    return gone


def _bucket() -> str:
    """Offsite backup bucket name (site_config saas_core_backup_bucket). No default: unset disables offsite copies."""
    try:
        import frappe
        return str(frappe.conf.get("saas_core_backup_bucket") or "")
    except Exception:  # noqa: BLE001
        return ""


def _conf(key, default):
    try:
        import frappe
        v = frappe.conf.get(key)
        return int(v) if v not in (None, "") else default
    except Exception:  # noqa: BLE001
        return default


OFFSITE_METHODS = ("bucket_list", "bucket_delete", "copy_backup_to_bucket")


def _offsite_enabled() -> bool:
    try:
        import frappe
        return str(frappe.conf.get("saas_core_offsite_backups") or "").lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def _record_failure(slug: str, site: str, err: str) -> None:
    """Audit + alert a backup problem. Never touches Tenant.status; every step is best effort."""
    import frappe
    try:
        from saas_core import audit
        audit.log("backup_failed", target_doctype="Tenant", target=slug, details={"site": site, "error": err})
    except Exception:  # noqa: BLE001
        pass
    try:
        frappe.log_error(f"daily backup failed for {slug}: {err}", "saas_core daily backup")
        to = frappe.conf.get("saas_core_alert_email")
        if to and callable(getattr(frappe, "sendmail", None)):
            frappe.sendmail(recipients=[to], subject=f"[ops] Daily backup failed: {slug}",
                            message=f"Backup of {site} failed: {err}. The tenant stays active.")
    except Exception:  # noqa: BLE001
        pass


def run_daily_backups(port=None, now: datetime | None = None) -> dict:
    """Scheduler entry: back up every active tenant's site via the port. Offsite copy + pruning only when
    `saas_core_offsite_backups` is on AND the port has the bucket methods (experimental). A backup problem is
    recorded (audit + alert) per tenant and never changes the tenant's status."""
    import logging
    import frappe
    from . import runner
    log = logging.getLogger(__name__)
    port = port or runner.get_port()
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    kd, kw = _conf("saas_core_backup_keep_daily", DEFAULT_KEEP_DAILY), _conf("saas_core_backup_keep_weekly", DEFAULT_KEEP_WEEKLY)
    offsite = False
    bucket = _bucket()
    if _offsite_enabled():
        if not bucket:
            log.warning("saas_core_offsite_backups is on but saas_core_backup_bucket is unset: skipping offsite copy")
        elif all(hasattr(port, m) for m in OFFSITE_METHODS):
            offsite = True
        else:
            log.warning("saas_core_offsite_backups is on but the port lacks bucket methods: offsite backups not configured, skipping")
            frappe.log_error("offsite backups not configured: port lacks bucket methods; skipped", "saas_core daily backup")
    out = {"ok": [], "failed": {}, "pruned": {}, "offsite": offsite}
    for t in frappe.get_all("Tenant", filters={"status": "active"}, fields=["name", "slug", "fc_site"]):
        slug, site = t.get("slug") or t.get("name"), t.get("fc_site")
        if not site:
            continue
        try:
            res = port.create_backup(site, workflow_id=f"daily-{slug}-{now:%Y%m%d}")
            if not res.ok:
                raise BackupError(res.message or "backup failed")
            if offsite:
                port.copy_backup_to_bucket(site, res.handle, bucket, f"tenants/{slug}/{stamp}/")
                out["pruned"][slug] = prune_tenant_backups(port, slug, bucket, kd, kw)
            out["ok"].append(slug)
        except Exception as e:  # noqa: BLE001 - one tenant must not stop the rest
            out["failed"][slug] = str(e)
            _record_failure(slug, site, str(e))
    return out
