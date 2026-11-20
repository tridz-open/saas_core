"""API timestamp formatting: every datetime leaves the API as ISO-8601 UTC with a trailing Z. Dependency-free on purpose."""
from __future__ import annotations

import datetime as _dt
from typing import Any


def _to_utc(d: _dt.datetime, utc: bool) -> _dt.datetime:
    """Naive datetime -> aware UTC. `utc=True`: the naive value is already UTC (billing fields); otherwise it is
    site-local (Frappe creation/modified/now_datetime) and is converted from the system timezone."""
    if d.tzinfo is not None:
        return d.astimezone(_dt.timezone.utc)
    if utc:
        return d.replace(tzinfo=_dt.timezone.utc)
    import zoneinfo

    import frappe
    try:
        tz = zoneinfo.ZoneInfo(frappe.utils.get_system_timezone() or "UTC")
    except Exception:  # noqa: BLE001 - unknown tz name: treat as UTC
        tz = _dt.timezone.utc
    return d.replace(tzinfo=tz).astimezone(_dt.timezone.utc)


def iso(v: Any, utc: bool = False) -> str | None:
    """ISO-8601 UTC with a trailing "Z" for every datetime the API emits. `utc=True` for fields stored as naive UTC
    (billing: period_*, issued_at, paid_at, trial_end, ...); the default treats naive values as site-local."""
    if v in (None, ""):
        return None
    if isinstance(v, str):
        try:
            v = _dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00")) if len(v.strip()) > 10 else v
        except ValueError:
            return v
    if isinstance(v, _dt.datetime):
        return _to_utc(v, utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, _dt.date):
        return v.isoformat()
    return str(v)
