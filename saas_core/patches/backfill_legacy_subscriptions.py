"""One-time: backfill payment id / paid amount / period start for pre-billing-release subscriptions, using only
locally stored data (no gateway calls). Gateway lookups stay in the manual command (see billing/backfill.py)."""


def execute():
    import frappe
    from saas_core.billing import backfill
    try:
        backfill.legacy_subscriptions(dry_run=False, use_gateway=False, actor="Administrator")
    except Exception:  # noqa: BLE001 - never block migrate
        frappe.log_error("saas_core legacy subscription backfill patch failed", "saas_core backfill")
