"""Template doc -> validated manifest dict for the fcctl port."""
from __future__ import annotations

import json

import frappe


class TemplateError(frappe.ValidationError):
    pass


def _j(v, default):
    if v in (None, ""):
        return default
    return json.loads(v) if isinstance(v, str) else v


def _get(doc, k, d=None):
    return doc.get(k, d) if hasattr(doc, "get") else getattr(doc, k, d)


PROTECTED_HOOKS = {"core.set_host_name": "the site host name", "core.set_fresh_encryption_key": "the encryption key"}
RELEASE_HOOKS = ("core.scheduler_resume", "core.maintenance_off")


def _public_plan_uses(slug) -> bool:
    try:
        import frappe
        return bool(frappe.get_all("Plan", filters={"template": slug, "is_public": 1}, limit=1))
    except Exception:  # noqa: BLE001 - advisory only; never block validation
        return False


def validate_template(doc) -> dict:
    """Validate a Template doc and return the manifest dict. Raises TemplateError."""
    slug = _get(doc, "slug") or _get(doc, "name")
    if not slug:
        raise TemplateError("template has no slug")
    apps_in = _get(doc, "apps") or []
    if not apps_in:
        raise TemplateError(f"template {slug}: at least one app is required")
    apps, seen = [], set()
    for i, a in enumerate(apps_in):
        name = _get(a, "app")
        if not name:
            raise TemplateError(f"template {slug}: app row {i + 1} has no app")
        if name in seen:
            raise TemplateError(f"template {slug}: duplicate app {name}")
        seen.add(name)
        apps.append({"app": name, "source_repo": _get(a, "source_repo"), "branch": _get(a, "branch"),
                     "version": _get(a, "version"), "install_order": int(_get(a, "install_order") or i + 1)})
    apps.sort(key=lambda a: a["install_order"])
    try:
        backup_source = _j(_get(doc, "backup_source"), None)
        hooks = _j(_get(doc, "hooks"), [])
        sanitize = _j(_get(doc, "sanitize_config"), {})
        checks = _j(_get(doc, "health_checks"), [])
        site_config = _j(_get(doc, "site_config"), {})
    except ValueError as e:
        raise TemplateError(f"template {slug}: invalid JSON ({e})")
    if not isinstance(hooks, list):
        raise TemplateError(f"template {slug}: hooks must be a list")
    for h in hooks:
        if not isinstance(h, dict) or not h.get("name") or h.get("version") in (None, ""):
            raise TemplateError(f"template {slug}: each hook needs name and version")
    if backup_source is not None:
        if not isinstance(backup_source, dict) or not (
                backup_source.get("profile") and backup_source.get("site") or backup_source.get("bucket_key")
                or backup_source.get("bucket")):
            raise TemplateError(f"template {slug}: backup_source needs profile+site(+backup), bucket_key or bucket")
        if backup_source.get("bucket") and isinstance(backup_source.get("manifest"), str):
            if not backup_source["manifest"].strip():
                raise TemplateError(f"template {slug}: backup_source.manifest path is empty")
            # manifest given as a bucket path: fetched and verified at restore time
        elif backup_source.get("bucket"):
            from .backups import BackupError, bucket_parts
            try:
                bucket_parts(backup_source)
            except BackupError as e:
                raise TemplateError(f"template {slug}: {e}")
    if not isinstance(site_config, dict) or not isinstance(sanitize, dict):
        raise TemplateError(f"template {slug}: site_config and sanitize_config must be objects")
    names = [h["name"] for h in hooks]
    for n in names:
        if n in PROTECTED_HOOKS:
            raise TemplateError(f"template {slug}: hook {n} is rejected by Pilot (protected config); remove it; "
                                f"Pilot manages {PROTECTED_HOOKS[n]}")
    site_admin = bool(_get(doc, "site_admin"))
    warnings = []
    missing = [n for n in RELEASE_HOOKS if n not in names]
    if missing:
        warnings.append(f"template {slug}: missing release hooks {', '.join(missing)}: fcctl releases isolation by "
                        "default from v0.1.x; add core.scheduler_resume and core.maintenance_off for older fcctl")
    if any(not n.startswith("core.") for n in names) and not site_admin:
        warnings.append(f"template {slug}: recipe hooks are present but site_admin is off; hooks using "
                        "ctx.site_admin (recipe hooks) will fail; enable site_admin")
    if not _get(doc, "is_public") and _public_plan_uses(slug):
        warnings.append(f"template {slug}: is_public is 0 but a public Plan references it; checkout rejects "
                        "non-public templates")
    return {"slug": slug, "recipe": _get(doc, "recipe"), "apps": apps, "backup_source": backup_source,
            "hooks": hooks, "sanitize_config": sanitize, "health_checks": checks, "site_config": site_config,
            "site_admin": site_admin, "warnings": warnings}
