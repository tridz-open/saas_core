"""C5: capacity preflight (FC trust tier) and plan-change guards. Pure functions; no frappe at import."""
from __future__ import annotations

import json

CAPACITY_EXHAUSTED = "capacity_exhausted"


class DowngradeRefused(Exception):
    code = "disk_shrink_not_supported"


def plan_monthly_cost(plan) -> float:
    """Monthly FC cost of a Plan doc/dict: limits.fc_monthly_cost (preferred) or a top-level fc_monthly_cost."""
    if plan is None:
        return 0.0
    g = plan.get if hasattr(plan, "get") else (lambda k, d=None: getattr(plan, k, d))
    lim = g("limits")
    if isinstance(lim, str):
        try:
            lim = json.loads(lim) if lim else {}
        except ValueError:
            lim = {}
    v = (lim or {}).get("fc_monthly_cost", g("fc_monthly_cost"))
    return float(v or 0)


def check_capacity(tier: dict | None, plan_cost: float = 0.0, new_resources: int = 1) -> str | None:
    """None if headroom exists, else a `capacity_exhausted: ...` message.
    tier: {spending_limit, running_monthly, resources_used, resource_limit} (missing keys are not enforced)."""
    if not tier:
        return None
    limit, running = tier.get("spending_limit"), tier.get("running_monthly")
    if limit is not None and running is not None and float(running) + float(plan_cost) > float(limit):
        return (f"{CAPACITY_EXHAUSTED}: running {float(running):g} + plan {float(plan_cost):g} exceeds "
                f"spending limit {float(limit):g}")
    used, cap = tier.get("resources_used"), tier.get("resource_limit")
    if used is not None and cap is not None and int(used) + new_resources > int(cap):
        return f"{CAPACITY_EXHAUSTED}: {int(used)} of {int(cap)} resources in use"
    return None


def preflight(port, plan=None) -> str | None:
    """Ask the port for the trust tier (optional method). Returns an error message or None."""
    fn = getattr(port, "trust_tier", None)
    if fn is None:
        return None
    return check_capacity(fn(), plan_monthly_cost(plan))


def assert_plan_change_allowed(old_limits: dict | None, new_limits: dict | None) -> None:
    """Refuse a plan change whose disk (limits.disk_gb) is smaller; FC cannot shrink a disk."""
    o, n = (old_limits or {}).get("disk_gb"), (new_limits or {}).get("disk_gb")
    if o is not None and n is not None and float(n) < float(o):
        raise DowngradeRefused(f"Target plan disk ({float(n):g} GB) is smaller than current ({float(o):g} GB); "
                               "disk shrink is not supported")
