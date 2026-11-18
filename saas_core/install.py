"""Install hooks: create platform roles idempotently."""
import frappe

GOLDEN_MSG = "saas_core must not be installed on a golden template site (site_config saas_golden set)"
ROLES = ("SaaS Admin", "SaaS Customer", "SaaS Platform Worker")


def before_install() -> None:
    if frappe.conf.get("saas_golden"):
        raise frappe.ValidationError(GOLDEN_MSG)


def after_install() -> None:
    for role in ROLES:
        if not frappe.db.exists("Role", role):
            frappe.get_doc({"doctype": "Role", "name": role, "role_name": role, "desk_access": 1 if role != "SaaS Customer" else 0}).insert(ignore_permissions=True)
    ensure_email_verified_field()
    ensure_session_key()


def ensure_session_key() -> None:
    """Generate a Fernet key into Platform Settings.session_key if absent. Idempotent."""
    try:
        doc = frappe.get_doc("Platform Settings")
    except frappe.DoesNotExistError:  # doctype not synced yet (should not happen after migrate)
        return
    if doc.get_password("session_key", raise_exception=False):
        return
    from cryptography.fernet import Fernet
    doc.session_key = Fernet.generate_key().decode()
    doc.save(ignore_permissions=True)


def ensure_email_verified_field() -> None:
    """Custom Field User.email_verified (Check); set by api.v1.verify_email.confirm. Idempotent."""
    if frappe.db.exists("Custom Field", "User-email_verified"):
        return
    frappe.get_doc({"doctype": "Custom Field", "name": "User-email_verified", "dt": "User", "fieldname": "email_verified", "fieldtype": "Check",
                    "label": "Email Verified", "default": "0", "read_only": 1, "insert_after": "enabled"}
                   ).insert(ignore_permissions=True)
