"""Restore enqueue after approval.
"""
from __future__ import annotations

import json


def enqueue_restore_after_approval(approval) -> str:
    """Enqueue the restore job for an approved/executing 'restore_backup' Approval Request. Returns job name."""
    from saas_core import jobs
    g = (lambda k: approval.get(k)) if hasattr(approval, "get") else (lambda k: getattr(approval, k, None))
    if g("op") != "restore_backup":
        raise ValueError("not a restore_backup approval")
    if g("status") not in ("approved", "executing"):
        raise ValueError("approval is not approved")
    args = g("args") or {}
    args = json.loads(args) if isinstance(args, str) else dict(args)
    args["restore"] = True
    tenant = g("target")
    return jobs.enqueue_job(tenant, "restore", args=args, approval=g("name"),
                            dedupe_key=f"restore:{tenant}:{args.get('backup')}")
