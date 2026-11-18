"""Create Razorpay Plans for Plan Price rows lacking a gateway_price_id.

    bench --site <site> execute saas_core.gateways.razorpay_sync.sync_plans [--kwargs "{'live': False}"]

Refuses a live-mode Gateway Account unless live=True. Prints only plan codes and plan ids;
API errors are recorded (redacted, no secrets) in the result and processing continues.
"""
import re

import frappe

from .base import GatewayError
from .registry import get_gateway

API = "https://api.razorpay.com/v1"
_PERIOD = {"month": "monthly", "year": "yearly"}
_SECRET_RE = re.compile(r"rzp_(?:test|live)_\w+|whsec_\w+")


def _resolve_account(account):
    if account:
        return account
    rows = frappe.get_all("Gateway Account", filters={"gateway": "razorpay", "enabled": 1}, pluck="name", limit=1)
    if not rows:
        frappe.throw("No enabled Razorpay Gateway Account")
    return rows[0]


def _clean_error(exc, gw):
    text = _SECRET_RE.sub("[redacted]", str(exc))
    for secret in (getattr(gw, "_key_secret", ""), getattr(gw, "_key_id", "")):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:300]


def sync_plans(live=False, account=None, http=None):
    """Returns {"synced": [{plan, interval, currency, plan_id}], "errors": [{plan, interval, currency, error}], "skipped": n}."""
    account = _resolve_account(account)
    acc = frappe.get_doc("Gateway Account", account)
    if acc.get("gateway") != "razorpay":
        frappe.throw(f"Gateway Account {account} is not a razorpay account")
    if acc.get("mode") == "live" and not live:
        frappe.throw("Gateway Account is in live mode; pass live=True to sync plans against live Razorpay")
    overrides = {"http": http} if http is not None else {}
    gw = get_gateway("razorpay", account, **overrides)

    result = {"synced": [], "errors": [], "skipped": 0}
    for code in frappe.get_all("Plan", pluck="name"):
        plan = frappe.get_doc("Plan", code)
        dirty = False
        for row in plan.get("prices") or []:
            if (row.get("gateway") or "razorpay") != "razorpay":
                continue
            if row.get("gateway_price_id"):
                result["skipped"] += 1
                continue
            interval = row.get("interval")
            currency = (row.get("currency") or "").upper()
            info = {"plan": plan.get("name") or code, "interval": interval, "currency": currency}
            if not currency:  # never default: a plan created in the wrong currency cannot be changed later
                result["errors"].append({**info, "error": "price row has no currency"})
                continue
            if interval not in _PERIOD or not int(row.get("amount") or 0):
                result["errors"].append({**info, "error": "invalid interval or amount"})
                continue
            body = {"period": _PERIOD[interval], "interval": 1,
                    "item": {"name": f"{plan.get('title') or code} ({interval})"[:100],
                             "amount": int(row.get("amount")), "currency": currency}}
            try:
                res = gw.http.request("POST", f"{API}/plans", json_body=body, basic_auth=gw._auth())
                plan_id = (res or {}).get("id")
                if not plan_id:
                    raise GatewayError("no plan id in response")
            except Exception as exc:  # record and continue; never echo secrets
                result["errors"].append({**info, "error": _clean_error(exc, gw)})
                continue
            row.set("gateway_price_id", plan_id) if hasattr(row, "set") else row.__setitem__("gateway_price_id", plan_id)
            if not row.get("gateway"):
                row.set("gateway", "razorpay") if hasattr(row, "set") else row.__setitem__("gateway", "razorpay")
            dirty = True
            result["synced"].append({**info, "plan_id": plan_id})
            print(f"{info['plan']} {interval} {currency} {plan_id}")
        if dirty:
            plan.save(ignore_permissions=True)
    for e in result["errors"]:
        print(f"{e['plan']} {e['interval']} {e['currency']} ERROR {e['error']}")
    frappe.db.commit()
    return result
