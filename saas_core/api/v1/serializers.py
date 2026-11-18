"""Serializers: DocType docs (or dict rows) -> OpenAPI schema dicts. Never emit secrets/internals."""
from __future__ import annotations

import datetime as _dt
from typing import Any

from ._common import as_dict, as_list


def _g(o: Any, k: str, d: Any = None) -> Any:
    if o is None:
        return d
    v = o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)
    return d if v is None else v


def iso(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return str(v)


def to_plan(doc: Any, available: bool | None = None) -> dict:
    prices = [{"interval": _g(r, "interval"), "amount": int(_g(r, "amount", 0)), "currency": _g(r, "currency")}
              for r in (_g(doc, "prices", []) or [])]
    avail = bool(_g(doc, "available", True)) if available is None else available
    return {
        "code": _g(doc, "code") or _g(doc, "name"),
        "title": _g(doc, "title", ""),
        "description": _g(doc, "description", "") or "",
        "prices": prices,
        "limits": as_dict(_g(doc, "limits")),
        "trial_days": int(_g(doc, "trial_days", 0) or 0),
        "template": _g(doc, "template"),
        "features": [str(x) for x in as_list(_g(doc, "features"))],
        "available": avail,
    }


def to_template(doc: Any, signup_schema: dict | None = None) -> dict:
    return {
        "slug": _g(doc, "slug") or _g(doc, "name"),
        "title": _g(doc, "title", ""),
        "description": _g(doc, "description", "") or "",
        "min_plan": _g(doc, "min_plan"),
        "signup_schema": signup_schema or {},
    }


def to_tenant(doc: Any) -> dict:
    site = _g(doc, "fc_site")
    active = _g(doc, "status") == "active"
    custom = _g(doc, "custom_domain")
    url = None
    if active:
        host = custom or site
        from saas_core.config import get_setting
        url = str(get_setting("tenant_url_template", "https://{site}")).format(site=host) if host else None
    return {
        "name": _g(doc, "name"),
        "slug": _g(doc, "slug") or _g(doc, "name"),
        "status": _g(doc, "status", "pending"),
        "plan": _g(doc, "plan"),
        "template": _g(doc, "template"),
        "region": _g(doc, "region"),
        "url": url,
        "custom_domain": custom,
        "created_at": iso(_g(doc, "creation")),
        "active_job": _g(doc, "active_job"),
        "suspend_reason": _g(doc, "suspend_reason"),
    }


_DONE = ("succeeded",)
_CUSTOMER_ERROR = "The operation failed. Our team has been notified."


def _job_started_at(doc: Any, steps: list) -> str | None:
    """When work began: explicit job started_at, else the earliest step start, else the job's creation."""
    explicit = iso(_g(doc, "started_at"))
    if explicit:
        return explicit
    starts = sorted(s["started_at"] for s in steps if s.get("started_at"))
    return starts[0] if starts else iso(_g(doc, "creation"))


def to_job(doc: Any) -> dict:
    steps = [{
        "seq": int(_g(s, "seq", 0) or 0), "name": _g(s, "step_name", None) or _g(s, "name", ""), "state": _g(s, "state", "pending"),
        "started_at": iso(_g(s, "started_at")), "finished_at": iso(_g(s, "finished_at")),
        "message": None,  # step messages may carry FC internals
    } for s in (_g(doc, "steps", []) or [])]
    for s in steps:  # additive aliases for portal clients that read {key, status}
        s["key"], s["status"] = s["name"], s["state"]
    state = _g(doc, "state", "pending")
    if state == "succeeded":
        progress = 1.0
    elif steps:
        progress = round(sum(1 for s in steps if s["state"] in _DONE) / len(steps), 4)
    else:
        progress = 0.0
    return {
        "name": _g(doc, "name"), "tenant": _g(doc, "tenant"), "kind": _g(doc, "kind"), "state": state,
        "progress": progress, "steps": steps, "attempts": int(_g(doc, "attempts", 0) or 0),
        "needs_attention": bool(_g(doc, "needs_attention", False)),
        "error": _CUSTOMER_ERROR if _g(doc, "error") else None,
        "created_at": iso(_g(doc, "creation")), "finished_at": iso(_g(doc, "finished_at")),
        "started_at": _job_started_at(doc, steps),
    }


def to_subscription(doc: Any) -> dict:
    out = {
        "tenant": _g(doc, "tenant"), "plan": _g(doc, "plan"), "status": _g(doc, "status"),
        "interval": _g(doc, "interval"),
        "period_end": iso(_g(doc, "period_end")),
        "cancel_at_period_end": bool(_g(doc, "cancel_at_period_end", False)),
        "dunning_stage": int(_g(doc, "dunning_stage", 0) or 0),
        "trial_end": iso(_g(doc, "trial_end")),
    }
    if _g(doc, "gateway"):
        out["gateway"] = _g(doc, "gateway")
    if out["interval"] is None:
        del out["interval"]
    out["payment_mocked"] = bool(_g(doc, "payment_mocked", False))
    return out


def to_invoice(doc: Any) -> dict:
    out = {
        "name": _g(doc, "name"), "tenant": _g(doc, "tenant"), "number": _g(doc, "number") or _g(doc, "name"),
        "status": _g(doc, "status"),
        "total": {"amount": int(_g(doc, "total_amount", 0) or 0), "currency": _g(doc, "currency", "")},
        "issued_at": iso(_g(doc, "issued_at")), "paid_at": iso(_g(doc, "paid_at")),
        "period_start": iso(_g(doc, "period_start")), "period_end": iso(_g(doc, "period_end")),
        "has_pdf": bool(_g(doc, "pdf_url")),
        "payment_mocked": bool(_g(doc, "payment_mocked", False)),
    }
    return {k: v for k, v in out.items() if v is not None or k == "paid_at"}


def to_backup(b: Any) -> dict:
    size = _g(b, "size_bytes")
    return {
        "name": _g(b, "name"), "created_at": iso(_g(b, "created_at")),
        "size_bytes": int(size) if size is not None else None,
        "kind": _g(b, "kind", "on_demand"),
        "parts": [str(p) for p in (_g(b, "parts", []) or [])],
    }


def to_ticket(doc: Any) -> dict:
    return {
        "name": _g(doc, "name"), "tenant": _g(doc, "tenant"), "subject": _g(doc, "subject", ""),
        "status": _g(doc, "status", "open"), "priority": _g(doc, "priority", "normal"),
        "created_at": iso(_g(doc, "creation")),
        "replies": [{"from_staff": bool(_g(r, "from_staff", False)), "body": _g(r, "body", ""),
                     "at": iso(_g(r, "at"))} for r in (_g(doc, "replies", []) or [])],
    }


def to_me(user: str, email: str | None = None, full_name: str | None = None, roles: list | None = None,
          tenant_count: int = 0, csrf_token: str | None = None, email_verified: bool | None = None) -> dict:
    out = {"user": user, "email": email or user, "full_name": full_name or "",
           "roles": [str(r) for r in (roles or [])], "tenant_count": int(tenant_count)}
    if csrf_token:
        out["csrf_token"] = csrf_token
    if email_verified is not None:
        out["email_verified"] = bool(email_verified)
    return out
