"""One-shot bootstrap of a Razorpay Gateway Account from site_config.

    bench --site <site> execute saas_core.gateways.bootstrap.from_site_config

Reads saas_core_razorpay_key_id / _key_secret / _webhook_secret, upserts the Gateway Account,
and returns {"account": name, "action": "created"|"updated"}. Never logs or prints values;
operator should delete the site_config keys afterwards.
"""
import frappe

KEY_ID, KEY_SECRET, WEBHOOK_SECRET = (
    "saas_core_razorpay_key_id", "saas_core_razorpay_key_secret", "saas_core_razorpay_webhook_secret")
_TEST_PREFIX = "rz" + "p_test_"  # split so no key-like literal sits in the source tree
_LIVE_PREFIX = "rz" + "p_live_"


def from_site_config(allow_live=False):
    conf = frappe.conf
    key_id, key_secret, webhook_secret = conf.get(KEY_ID), conf.get(KEY_SECRET), conf.get(WEBHOOK_SECRET)
    missing = [k for k, v in ((KEY_ID, key_id), (KEY_SECRET, key_secret), (WEBHOOK_SECRET, webhook_secret)) if not v]
    if missing:
        frappe.throw("Missing site_config keys: " + ", ".join(missing))  # names only, never values
    live = str(key_id).startswith(_LIVE_PREFIX)
    if not allow_live and not str(key_id).startswith(_TEST_PREFIX):
        frappe.throw("Razorpay key_id is not a test key; refusing (pass allow_live=True to override)")
    mode = "live" if allow_live and live else "test"
    account_name = f"razorpay-{mode}"

    name = frappe.db.get_value("Gateway Account", {"gateway": "razorpay", "mode": mode, "account_name": account_name})
    values = {"key_id": key_id, "key_secret": key_secret, "webhook_secret": webhook_secret, "enabled": 1}
    if name:
        doc = frappe.get_doc("Gateway Account", name)
        doc.update(values)
        doc.save(ignore_permissions=True)
        action = "updated"
    else:
        doc = frappe.get_doc({"doctype": "Gateway Account", "account_name": account_name,
                              "gateway": "razorpay", "mode": mode, **values})
        doc.insert(ignore_permissions=True)
        action = "created"
    frappe.db.commit()
    return {"account": doc.name, "action": action}
