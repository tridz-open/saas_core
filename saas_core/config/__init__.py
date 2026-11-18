"""Config accessor. Order: site_config `saas_core_<key>` -> Platform Settings -> default."""
import frappe

_PASSWORD_KEYS = frozenset({"session_key"})
DEFAULTS = {"payment_banner": "Test mode: payments mocked", "public_signup": True, "capacity_open": True}
_UNSET = object()


def _blank(v) -> bool:
    return v is None or v == ""


def _from_settings(key: str):
    try:
        if key in _PASSWORD_KEYS:
            doc = frappe.get_doc("Platform Settings")
            return doc.get_password(key, raise_exception=False)
        return frappe.db.get_single_value("Platform Settings", key)
    except Exception:
        return None


def get_setting(key: str, default=_UNSET):
    v = frappe.conf.get(f"saas_core_{key}")
    if not _blank(v):
        return v
    v = _from_settings(key)
    if not _blank(v) and True:
        return v
    if default is not _UNSET:
        return default
    return DEFAULTS.get(key)
