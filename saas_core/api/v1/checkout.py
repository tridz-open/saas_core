"""checkout.create_session: hosted-checkout session via the configured gateway."""
from __future__ import annotations

import re
import types

import frappe

from saas_core.config import get_setting
from ._common import (ApiError, as_dict, endpoint, idempotency_key, idempotent, json_body, rate_limit,
                      request_hash, require_fields, require_login, set_status, tenant_access, gateway_for, iso_or_none)

_SLUG = re.compile(r"^[a-z][a-z0-9-]{2,30}[a-z0-9]$")
GATEWAYS = ("razorpay", "stripe", "manual")
LIVE_STATUSES = ("trialing", "active", "past_due")


def _checkout_mode(gw_name):
    """Gateway Account checkout_mode (order|subscription); defaults to order, the live-verified mode.
    Razorpay `subscription` mode is EXPERIMENTAL and needs saas_core_experimental=razorpay_subscription."""
    try:
        mode = frappe.db.get_value("Gateway Account", {"gateway": gw_name, "enabled": 1}, "checkout_mode")
    except Exception:
        mode = None
    if mode == "subscription":
        if gw_name == "razorpay":
            from saas_core.experimental import enabled
            if not enabled("razorpay_subscription"):
                return "order"
        return "subscription"
    return "order"


def _amount_minor(price):
    """Plan Price.amount is already in minor units (never client input)."""
    try:
        return int(round(float(price.get("amount") or 0)))
    except (TypeError, ValueError):
        return 0


def _pick_price(plan, interval, currency, gateway):
    rows = [r for r in (plan.get("prices") or []) if r.get("interval") == interval]
    if currency:
        rows = [r for r in rows if r.get("currency") == currency]
    if gateway:
        gw = [r for r in rows if not r.get("gateway") or r.get("gateway") == gateway]
        rows = gw or rows
    if not rows:
        raise ApiError("validation_error", "No price for this plan/interval/currency", 422,
                       {"interval": "no matching price"})
    return rows[0]


@endpoint()
def create_session(plan=None, interval=None, success_url=None, cancel_url=None, tenant=None, slug=None,
                   template=None, currency=None, gateway=None, recipe_fields=None):
    body = json_body(locals())
    user = require_login()
    from saas_core.config.signup import is_email_verified
    if not is_email_verified(user):
        raise ApiError("email_unverified", "Verify your email first", 403)
    require_fields(body, "plan", "interval", "success_url", "cancel_url")
    rate_limit(f"checkout:{user}", 20, 3600)
    errs = {}
    if body["interval"] not in ("month", "year"):
        errs["interval"] = "must be month or year"
    if body.get("slug") and not _SLUG.match(str(body["slug"])):
        errs["slug"] = "invalid slug"
    if body.get("gateway") and body["gateway"] not in GATEWAYS:
        errs["gateway"] = "unsupported gateway"
    for u in ("success_url", "cancel_url"):
        if not str(body[u]).startswith(("https://", "http://")):
            errs[u] = "must be an absolute URL"
    if errs:
        raise ApiError("validation_error", "Invalid checkout request", 422, errs)
    if not body.get("tenant") and not body.get("slug"):
        raise ApiError("bad_request", "tenant or slug is required", 400, {"slug": "required"})

    def run():
        if not frappe.db.exists("Plan", body["plan"]):
            raise ApiError("not_found", "Plan not found", 404)
        plan_doc = frappe.get_doc("Plan", body["plan"])
        from ._selection import check_selectable
        tenant_name = body.get("tenant")
        # An existing tenant keeps the (possibly operator-assigned) plan/template it already has; anything
        # else a customer picks must be public/available.
        if not (tenant_name and frappe.db.get_value("Tenant", tenant_name, "plan") == body["plan"]):
            check_selectable(user, body["plan"], body.get("template") or plan_doc.get("template"))
        if tenant_name:
            tenant_access(tenant_name, user, "write")
            # P1-2: a tenant with a live subscription changes plan via change_plan; a second checkout would
            # create a parallel subscription whose payments could reset the first one's dunning.
            if frappe.get_all("Subscription", filters={"tenant": tenant_name,
                                                       "status": ["in", list(LIVE_STATUSES)]}, pluck="name"):
                raise ApiError("conflict", "Tenant already has a subscription; use change plan", 409)
        else:
            from saas_core.billing.entitlements import can_create_tenant
            ok, reason = can_create_tenant(user, body["plan"])
            if not ok:
                if "capacity" in str(reason).lower():
                    raise ApiError("capacity_unavailable", str(reason), 409)
                raise ApiError("validation_error", str(reason or "Not allowed"), 422)
            if frappe.db.exists("Tenant", body["slug"]):
                raise ApiError("conflict", "That site name is already taken", 409, {"slug": "taken"})
            tdoc = frappe.get_doc({
                "doctype": "Tenant", "name": body["slug"], "slug": body["slug"], "owner_user": user,
                "status": "pending", "plan": body["plan"], "template": body.get("template") or plan_doc.get("template"),
                "created_via": "checkout", "recipe_fields": frappe.as_json(as_dict(body.get("recipe_fields"))),
            })
            tdoc.insert(ignore_permissions=True)
            tenant_name = tdoc.get("name")
        from saas_core.gateways.policy import resolve_gateway
        gw_name = resolve_gateway(body.get("gateway"), user)  # P0-2: client choice ignored unless default/admin
        price = _pick_price(plan_doc, body["interval"], body.get("currency"), gw_name)
        gw = gateway_for(gw_name)
        mode = _checkout_mode(gw_name)
        fields = dict(plan=body["plan"], gateway_price_id=price.get("gateway_price_id"), tenant=tenant_name,
                      customer_email=frappe.db.get_value("User", user, "email") or user,
                      success_url=body["success_url"], cancel_url=body["cancel_url"],
                      trial_days=int(plan_doc.get("trial_days") or 0),
                      metadata={"tenant": tenant_name, "plan": body["plan"], "user": user,
                                "interval": body["interval"], "currency": price.get("currency"),
                                "mode": mode, "amount_minor": _amount_minor(price)})

        def build(f):
            try:
                from saas_core.gateways.base import CheckoutParams
                return CheckoutParams(**f)
            except ImportError:
                return types.SimpleNamespace(**f)

        def as_invoice_fallback():
            return {**fields, "metadata": {**fields["metadata"], "mode": "order", "billing_mode": "invoice"}}

        def gateway_error():
            frappe.log_error("saas_core checkout gateway error")
            return ApiError("gateway_error", "Payment provider unavailable, try again", 502)

        from saas_core.gateways.base import GatewayError
        # Razorpay: recurring subscriptions are primary; if one cannot be created (no plan_id mapped, or the
        # API call fails) fall back to a one-time order for the first period + recurring payment-link invoices.
        can_fallback = gw_name == "razorpay" and mode == "subscription"
        fallback = False
        try:
            if can_fallback and not price.get("gateway_price_id"):
                raise GatewayError("no gateway plan id for this price")
            res = gw.create_checkout_session(build(fields))
        except ApiError:
            raise
        except Exception as e:
            if not (can_fallback and isinstance(e, GatewayError)):
                raise gateway_error()
            frappe.log_error("saas_core checkout: razorpay subscription creation failed, using invoice fallback")
            try:
                res = gw.create_checkout_session(build(as_invoice_fallback()))
            except ApiError:
                raise
            except Exception:
                raise gateway_error()
            fallback = True
        sess_ref = str(getattr(res, "session_ref", None) or getattr(res, "session_id", "") or "")
        if sess_ref:
            from saas_core.billing.checkout_sessions import store_session
            meta_used = (as_invoice_fallback() if fallback else fields)["metadata"]
            try:
                row_idx = (plan_doc.get("prices") or []).index(price)
            except ValueError:
                row_idx = -1
            store_session(gw_name, sess_ref, tenant=tenant_name, owner_user=user, plan=body["plan"],
                          interval=body["interval"], currency=price.get("currency"),
                          billing_mode="invoice" if (fallback or meta_used.get("mode") == "order") else "subscription",
                          checkout_mode=meta_used.get("mode"), amount_minor=_amount_minor(price),
                          gateway_price_id=price.get("gateway_price_id") or "", price_row=row_idx)
        set_status(201)
        out = {"gateway": gw_name, "session_id": str(getattr(res, "session_ref", None) or getattr(res, "session_id", "")),
               "url": str(getattr(res, "url", "")), "tenant": tenant_name}
        mocked = bool(getattr(res, "payment_mocked", False) or (getattr(res, "raw", None) or {}).get("payment_mocked"))
        out["payment_mocked"] = mocked
        if fallback:
            out["fallback"] = "invoice"
        if mocked:
            out["subscription_status"] = "active"
        try:
            cp = gw.client_payload(res)
            if cp:
                out["client_payload"] = cp
        except (NotImplementedError, AttributeError):
            pass
        pk = getattr(res, "public_key", None) or (getattr(res, "raw", None) or {}).get("public_key")
        if pk:
            out["public_key"] = pk
        exp = iso_or_none(getattr(res, "expires_at", None))
        if exp:
            out["expires_at"] = exp
        return out

    return idempotent(idempotency_key(), request_hash(body), run, scope="checkout")


from .checkout_verify import verify_payment  # noqa: E402,F401  (public path saas_core.api.v1.checkout.verify_payment)
