"""Reconcile stuck / outcome_unknown jobs by asking the port by workflow_id (idempotent pass)."""
from __future__ import annotations

import frappe

from .ports import ACTIVE_STATES
from .runner import RUN_JOB_NAME, _Run, get_port
from .stores import FrappeApprovalStore, FrappeJournal


DEFAULT_MAX_ATTEMPTS = 10


def _max_attempts() -> int:
    """Auto-retry cap for reconcile; override with site config `saas_core_max_job_attempts`."""
    try:
        return int(frappe.conf.get("saas_core_max_job_attempts") or DEFAULT_MAX_ATTEMPTS)
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_MAX_ATTEMPTS


def _old_enough(job, min_age_seconds: float) -> bool:
    from frappe.utils import now_datetime, time_diff_in_seconds
    m = job.get("modified")
    return not m or time_diff_in_seconds(now_datetime(), m) >= min_age_seconds


def run(port=None, min_age_seconds: float = 0, stuck_after_seconds: float = 1800) -> dict:
    """Returns {job: action}. Provision: adopt finished workflows, mark failed, re-run if the port never saw it.
    terminate/backup with unknown outcome are left needs_attention (never auto-retried)."""
    port = port or get_port()
    out: dict[str, str] = {}
    cap = _max_attempts()
    fields = ["name", "state", "kind", "workflow_id", "modified", "attempts", "error"]
    for row in frappe.get_all("Provision Job", fields=fields):
        state = row.get("state")
        if state not in ACTIVE_STATES or not _old_enough(row, min_age_seconds if state != "outcome_unknown" else 0):
            continue
        name, kind, wf = row["name"], row.get("kind"), row.get("workflow_id")
        attempts = int(row.get("attempts") or 0)
        if state == "outcome_unknown" and attempts >= cap:
            # A deterministic error (e.g. fcctl ResumeRefused, a bad FC plan) never clears by itself:
            # stop re-enqueueing it every sweep and hand it to a human. Tenant status is left untouched.
            FrappeJournal().set_job_state(
                name, "needs_attention",
                error=f"auto-retry limit reached after {attempts} attempts; last error: {row.get('error') or ''}"[:1000])
            out[name] = "needs_attention"
            continue
        r = _Run(name, port, FrappeJournal(), FrappeApprovalStore())
        journal = r.j
        if kind == "provision" and wf:
            found = port.find_workflow(wf)
            if found is not None and found.state == "succeeded":
                for s in journal.steps(name):
                    if s.get("state") in ("dispatching", "outcome_unknown", "submitted", "running"):
                        journal.transition(name, s["seq"], "succeeded", message="reconciled")
                r.complete_provision(found)
                out[name] = "adopted"
            elif found is not None and found.state == "failed":
                r.fail(found.message or "workflow failed")
                out[name] = "failed"
            elif found is not None:
                if state == "running" and _old_enough(row, stuck_after_seconds):
                    journal.set_job_state(name, "stuck")
                    out[name] = "stuck"
                else:
                    frappe.enqueue(RUN_JOB_NAME, job=name)  # port resumes by workflow_id
                    out[name] = "resumed"
            else:
                journal.set_job_state(name, "pending")
                frappe.enqueue(RUN_JOB_NAME, job=name)
                out[name] = "redispatched"
        elif kind in ("terminate", "backup", "restore") and state in ("outcome_unknown", "dispatching"):
            journal.set_job_state(name, "needs_attention", error="outcome unknown; human review required")
            out[name] = "needs_attention"
        elif kind in ("suspend", "resume", "change_plan"):
            journal.set_job_state(name, "pending")
            frappe.enqueue(RUN_JOB_NAME, job=name)
            out[name] = "redispatched"
    return out
