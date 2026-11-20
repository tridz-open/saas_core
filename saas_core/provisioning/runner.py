"""Job state machine. run_job(job) is resume-safe: before dispatching a provision it asks the port by workflow_id."""
from __future__ import annotations

import importlib
import json
import re

import frappe

audit = importlib.import_module("saas_core.audit")
events = importlib.import_module("saas_core.events")
registry = importlib.import_module("saas_core.recipes.registry")
from saas_core.config import get_setting
from .ports import (ACTIVE_STATES, ApprovalError, FcctlPort, ProvisionSpec, RunResult, TERMINAL_STATES)
from .stores import FcctlAdapter, FrappeApprovalStore, FrappeJournal

_port: FcctlPort | None = None


def get_port() -> FcctlPort:
    """FakeFcctl when site_config saas_core_fake_fcctl is truthy, else the real adapter. Tests may set_port()."""
    global _port
    if _port is None:
        if get_setting("local_bench", False):  # EXPERIMENTAL, DEV ONLY: real local sites on this bench
            from saas_core.experimental import require
            require("local_bench")
            from .local_bench import LocalBenchPort
            _port = LocalBenchPort()
        elif get_setting("fake_fcctl"):
            from .fake_fcctl import FakeFcctl
            _port = FakeFcctl()
        else:  # real Frappe Cloud
            from .fc_client import build_fcctl_client, build_fcctl_journal, require_config
            _, server = require_config()
            _port = FcctlAdapter(client=build_fcctl_client(), server=server, journal=build_fcctl_journal())
    return _port


def set_port(port: FcctlPort | None) -> None:
    global _port
    _port = port


RUN_JOB_NAME = "saas_core.provisioning.runner.run_job"


class JobError(Exception):
    pass


def _json(v, default):
    if v in (None, ""):
        return default
    return json.loads(v) if isinstance(v, str) else v


def site_name(slug: str) -> str:
    """Full site name under the configured domain, or the bare subdomain when site_domain is unset (FC picks its default)."""
    domain = (get_setting("site_domain") or "").strip().lstrip(".")
    s = re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-")
    return f"{s}.{domain}" if domain else s


def build_spec(tenant, workflow_id: str) -> ProvisionSpec:
    from .templates import validate_template
    if not tenant.get("template"):
        raise JobError("tenant has no template")
    manifest = validate_template(frappe.get_doc("Template", tenant.get("template")))
    plan = frappe.get_doc("Plan", tenant.get("plan")) if tenant.get("plan") and frappe.db.exists("Plan", tenant.get("plan")) else None
    config = dict(manifest.get("site_config") or {})
    recipe = registry.get(manifest.get("recipe"))
    if recipe is not None:
        config.update(recipe.tenant_config(tenant, plan, _json(tenant.get("recipe_fields"), {})) or {})
    return ProvisionSpec(workflow_id=workflow_id, site=site_name(tenant.get("slug") or tenant.name),
                         domain=(get_setting("site_domain") or "").strip().lstrip(".") or None,
                         tenant=tenant.name, plan=plan.get("fc_server_plan") if plan else None,
                         region=tenant.get("region"), manifest=manifest, config=config, hooks=manifest.get("hooks") or [])


def _set_tenant(tenant: str, **vals):
    for k, v in vals.items():
        frappe.db.set_value("Tenant", tenant, k, v)


class _Run:
    def __init__(self, job_name: str, port: FcctlPort, journal: FrappeJournal, approvals: FrappeApprovalStore):
        self.name, self.port, self.j, self.ap = job_name, port, journal, approvals
        self.job = frappe.get_doc("Provision Job", job_name)
        self.tenant = frappe.get_doc("Tenant", self.job.get("tenant"))
        self.args = _json(self.job.get("args"), {})

    # -- helpers
    def close_interrupted(self):
        """A step left in `dispatching` by a killed worker is outcome_unknown; the resumed run supersedes it."""
        for s in self.j.steps(self.name):
            if s.get("state") == "dispatching":
                self.j.transition(self.name, s["seq"], "outcome_unknown", message="interrupted")
                self.j.transition(self.name, s["seq"], "failed", message="interrupted; superseded on resume")

    def event(self, t: str, **data):
        events.publish(t, tenant=self.tenant.name, job=self.name, data=data)

    def fail(self, msg: str, seq: int | None = None, tenant_status: str | None = "failed"):
        if seq is not None:
            self.j.transition(self.name, seq, "failed", message=msg)
        self.j.set_job_state(self.name, "needs_attention", error=msg)
        if tenant_status:
            _set_tenant(self.tenant.name, status=tenant_status)
        audit.log("job.needs_attention", target_doctype="Provision Job", target=self.name, details={"error": msg})
        self.event("job.needs_attention", error=msg)

    def dispatch(self, step: str, call, *, resume_seq: int | None = None) -> tuple[int, RunResult | None]:
        """Journal pending -> dispatching (persisted) -> call -> submitted. Unexpected exception => outcome_unknown."""
        seq = resume_seq or self.j.begin(self.name, step)
        self.j.transition(self.name, seq, "dispatching")
        self.j.set_job_state(self.name, "dispatching")
        try:
            res = call()
        except Exception as e:  # noqa: BLE001 - effect unknown: reconcile decides
            msg = f"outcome unknown: {e}"
            self.j.transition(self.name, seq, "outcome_unknown", message=msg)
            self.j.set_job_state(self.name, "outcome_unknown", error=msg)
            self.event("job.outcome_unknown", step=step)
            return seq, None
        self.j.transition(self.name, seq, "submitted", handle=res.handle)
        self.j.set_job_state(self.name, "submitted")
        return seq, res

    # -- kinds
    def provision(self):
        t = self.tenant
        wf = self.job.get("workflow_id") or t.get("workflow_id") or f"wf-{self.name}"
        if not self.job.get("workflow_id"):
            self.job.workflow_id = wf
            frappe.db.set_value("Provision Job", self.name, "workflow_id", wf)
        _set_tenant(t.name, status="provisioning", workflow_id=wf, active_job=self.name)
        try:
            spec = build_spec(t, wf)
        except Exception as e:  # noqa: BLE001
            return self.fail(f"invalid template/config: {e}")
        existing = self.port.find_workflow(wf)            # resume rule: ask by workflow_id first
        if existing is None:                               # C5: new server only; resumes are already counted
            from .preflight import preflight
            plan = frappe.get_doc("Plan", t.get("plan")) if t.get("plan") and frappe.db.exists("Plan", t.get("plan")) else None
            msg = preflight(self.port, plan)
            if msg:
                self.event("job.capacity_exhausted", error=msg)
                return self.fail(msg, tenant_status="pending")
        if existing is not None and existing.state == "succeeded":
            seq = self.j.begin(self.name, "adopt_existing_workflow")
            self.j.transition(self.name, seq, "dispatching")
            self.j.transition(self.name, seq, "submitted", handle=existing.handle)
            res = existing
        elif existing is not None and existing.state == "failed":
            return self.fail(existing.message or "workflow failed")
        else:                                              # None (not started) or running (port resumes by id)
            seq, res = self.dispatch("provision_site", lambda: self.port.provision_site(spec))
            if res is None:
                return
        if not res.ok or res.state == "failed":
            return self.fail(res.message or "provision failed", seq)
        if res.state == "running":
            self.j.transition(self.name, seq, "running")
            self.j.set_job_state(self.name, "running")
            return
        self.j.transition(self.name, seq, "succeeded", message=res.message)
        self.complete_provision(res)

    def complete_provision(self, res: RunResult):
        # fc_site is the actual host FC returned (may be FC's default domain); the API derives `url` from it
        _set_tenant(self.tenant.name, status="active", fc_site=res.site, suspend_reason=None)
        self.j.set_job_state(self.name, "succeeded")
        audit.log("tenant.provisioned", target_doctype="Tenant", target=self.tenant.name, details={"site": res.site})
        self.event("tenant.active", site=res.site)
        self._set_admin_credentials(res.site)

    def _setup_secret(self) -> str:
        """Per-tenant HMAC secret for the tenant's `setup_complete` callback. Generated once, stored as a Password
        field on the Tenant; re-runs reuse it so a site already holding it stays valid."""
        name = self.tenant.name
        try:
            existing = frappe.get_doc("Tenant", name).get_password("setup_secret", raise_exception=False)
        except Exception:  # noqa: BLE001 - missing key/doc: mint a new one
            existing = None
        if existing:
            return existing
        secret = frappe.generate_hash(length=48)
        doc = frappe.get_doc("Tenant", name)
        doc.setup_secret = secret
        doc.save(ignore_permissions=True)
        return secret

    def _write_site_setup_config(self, site) -> bool:
        """Hand the tenant site what the tenant-side setup app needs to call us back (secret never logged or audited)."""
        cp_url = get_setting("cp_url", None) or ""
        try:
            cp_url = (cp_url or frappe.utils.get_url() or "").rstrip("/")
            secret = self._setup_secret()
            cfg = {}
            # primary prefix `saas_setup`; site_config saas_core_setup_config_prefixes (comma list) adds aliases
            # for tenant-side setup apps that read a differently named key pair.
            extra = [x.strip() for x in str(get_setting("setup_config_prefixes", "") or "").split(",") if x.strip()]
            for prefix in ["saas_setup", *extra]:
                cfg[f"{prefix}_cp_url"], cfg[f"{prefix}_cp_secret"] = cp_url, secret
            self.port.set_site_config(site, cfg)
        except AttributeError:
            return True   # port without site-config support (older adapters): nothing to write
        except Exception as e:  # noqa: BLE001 - never include secrets in the message
            audit.log("tenant.setup_config_failed", target_doctype="Tenant", target=self.tenant.name,
                      details={"error": type(e).__name__})
            return False
        return True

    def _set_admin_credentials(self, site):
        """Write the setup callback config into the tenant's site_config, set a random (never stored, never mailed)
        Administrator password, then enqueue the "Complete your setup" mail job. The one-time setup link is minted
        INSIDE that job (welcome.send_tenant_setup), so it is never a job argument. Skipped entirely once that mail
        went out: a re-run must not reset the password behind the owner's back (use welcome.resend_setup_link for a
        deliberate resend). The welcome mail itself is sent later, by tenants.setup_complete."""
        if frappe.db.get_value("Tenant", self.tenant.name, "setup_email_sent_at"):
            return
        if not self._write_site_setup_config(site):
            return
        try:
            self.port.set_admin_password(site, frappe.generate_hash(length=16))
        except AttributeError:
            return  # port without admin-password support: nothing to mail
        except Exception as e:  # noqa: BLE001 - never include secrets in the message
            audit.log("tenant.admin_setup_link_failed", target_doctype="Tenant", target=self.tenant.name,
                      details={"error": type(e).__name__})
            return
        frappe.enqueue("saas_core.notifications.welcome.send_tenant_setup", tenant=self.tenant.name,
                       enqueue_after_commit=True)

    def simple(self, step: str, call_name: str, tenant_vals: dict | None, **kw):
        site = self.tenant.get("fc_site")
        if not site:
            return self.fail("tenant has no site", tenant_status=None)
        wf = self.job.get("workflow_id") or f"wf-{self.name}"
        if not self.job.get("workflow_id"):
            self.job.workflow_id = wf
            frappe.db.set_value("Provision Job", self.name, "workflow_id", wf)
        seq, res = self.dispatch(step, lambda: getattr(self.port, call_name)(site, workflow_id=wf, **kw))
        if res is None:
            return None
        if not res.ok:
            self.fail(res.message or f"{step} failed", seq, tenant_status=None)
            return None
        self.j.transition(self.name, seq, "succeeded", message=res.message)
        if tenant_vals:
            _set_tenant(self.tenant.name, **tenant_vals)
        self.j.set_job_state(self.name, "succeeded")
        audit.log(f"tenant.{step}", target_doctype="Tenant", target=self.tenant.name, details=self.args)
        self.event(f"tenant.{step}")
        return res

    def terminate(self):
        site = self.tenant.get("fc_site")
        appr = self.job.get("approval")
        try:
            if not appr:
                raise ApprovalError("terminate requires an approval")
            self.ap.assert_executable(appr, "terminate", self.tenant.name, self.args.get("approval_args"))
        except ApprovalError as e:
            return self.fail(f"approval check failed: {e}", tenant_status=None)
        self.ap.mark(appr, "executing", journal_id=self.name)
        _set_tenant(self.tenant.name, status="terminating")
        res = self.simple("terminate_site", "terminate_site", {"status": "terminating", "fc_site": None})
        if res is not None:
            self.ap.mark(appr, "executed")
        else:
            state = frappe.get_doc("Provision Job", self.name).get("state")
            self.ap.mark(appr, "outcome_unknown" if state == "outcome_unknown" else "failed")

    def backup(self):
        res = self.simple("create_backup", "create_backup", None)
        if res is not None:
            # journal saves the job concurrently -> self.job is stale; write the field directly
            frappe.db.set_value("Provision Job", self.name, "args", json.dumps({**self.args, "backup": res.handle}))
            frappe.db.commit()

    def change_plan(self):
        plan = self.args.get("plan")
        if not plan:
            return self.fail("change_plan needs args.plan", tenant_status=None)
        from .preflight import DowngradeRefused, assert_plan_change_allowed
        try:
            lim = lambda p: _json(frappe.db.get_value("Plan", p, "limits"), {})  # noqa: E731
            if self.tenant.get("plan") and self.tenant.get("plan") != plan:
                assert_plan_change_allowed(lim(self.tenant.get("plan")), lim(plan))
        except DowngradeRefused as e:
            return self.fail(f"disk_shrink_not_supported: {e}", tenant_status=None)
        fc_plan = frappe.db.get_value("Plan", plan, "fc_server_plan") or plan
        self.simple("change_plan", "change_plan", {"plan": plan}, plan=fc_plan)

    def restore(self):
        """Guarded restore: needs an executable approval (workflow-owned exemption: the approval gates the op)."""
        appr = self.job.get("approval")
        backup = self.args.get("backup")
        try:
            if not appr:
                raise ApprovalError("restore requires an approval")
            self.ap.assert_executable(appr, "restore_backup", self.tenant.name,
                                      self.args.get("approval_args") or {"backup": backup, "restore": True})
        except ApprovalError as e:
            return self.fail(f"approval check failed: {e}", tenant_status=None)
        if not backup:
            return self.fail("restore needs args.backup", tenant_status=None)
        if not hasattr(self.port, "restore_backup"):
            return self.fail("port does not support restore_backup", tenant_status=None)
        self.ap.mark(appr, "executing", journal_id=self.name)
        res = self.simple("restore_backup", "restore_backup", None, backup=backup)
        if res is not None:
            self.ap.mark(appr, "executed")
        else:
            state = frappe.get_doc("Provision Job", self.name).get("state")
            self.ap.mark(appr, "outcome_unknown" if state == "outcome_unknown" else "failed")

    def go(self):
        k = self.job.get("kind")
        if k == "provision":
            return self.provision()
        if k == "suspend":
            return self.simple("suspend_site", "suspend_site", {"status": "suspended",
                               "suspend_reason": self.args.get("reason") or "suspended"})
        if k == "resume":
            return self.simple("resume_site", "resume_site", {"status": "active", "suspend_reason": None})
        if k == "terminate":
            return self.terminate()
        if k == "change_plan":
            return self.change_plan()
        if k == "backup":
            return self.backup()
        if k == "restore":
            from saas_core.experimental import enabled
            if not enabled("restore"):
                return self.fail("restore is experimental and disabled (site_config saas_core_experimental=restore)",
                                 tenant_status=None)
            return self.restore()
        return self.fail(f"unsupported job kind {k}", tenant_status=None)


def _effective_at(args):
    """Parsed args.effective_at as naive datetime, or None."""
    v = _json(args, {}).get("effective_at") if args not in (None, "") else None
    if not v:
        return None
    from frappe.utils import get_datetime
    try:
        d = get_datetime(v)
    except Exception:  # noqa: BLE001 - unparseable: run now rather than strand the job
        return None
    return d.replace(tzinfo=None) if getattr(d, "tzinfo", None) else d


def run_due_scheduled() -> list[str]:
    """Scheduler hook: re-enqueue pending jobs whose args.effective_at has passed."""
    from saas_core.billing.proration import naive_utc_now
    now, out = naive_utc_now(), []  # effective_at is naive UTC (from period_end), not site-local
    for row in frappe.get_all("Provision Job", filters={"state": "pending"}, fields=["name", "args"]):
        at = _effective_at(row.get("args"))
        if at is not None and at <= now:
            frappe.enqueue(RUN_JOB_NAME, queue="long", timeout=3600, enqueue_after_commit=True, job=row["name"])
            out.append(row["name"])
    return out


def _fc_paused() -> bool:
    fn = getattr(importlib.import_module("saas_core.jobs"), "fc_is_paused", None)
    return bool(fn()) if callable(fn) else False


def run_job(job: str, port: FcctlPort | None = None) -> str:
    """Execute (or resume) a Provision Job. Returns the resulting job state."""
    port = port or get_port()
    try:
        cur = frappe.get_doc("Provision Job", job)
    except frappe.DoesNotExistError:
        return "missing"
    if cur.get("state") in TERMINAL_STATES:
        return cur.get("state")
    if _fc_paused():  # FC session lost: leave the job exactly as it is; fc_unpause() re-enqueues pending jobs
        return cur.get("state")
    if cur.get("state") == "pending":
        at = _effective_at(cur.get("args"))
        from saas_core.billing.proration import naive_utc_now
        if at is not None and at > naive_utc_now():
            return "pending"  # not yet due; run_due_scheduled() re-enqueues it at/after effective_at
    try:
        with audit.cache_lock(f"saas_core:job:{job}", ttl=3600, wait=0):
            r = _Run(job, port, FrappeJournal(), FrappeApprovalStore())
            r.job.attempts = int(r.job.get("attempts") or 0) + 1
            r.job.save(ignore_permissions=True)
            r.close_interrupted()  # resume: provision asks the port by workflow_id; other ops are idempotent by it
            r.go()
    except audit.LockTimeout:
        return "locked"
    return frappe.get_doc("Provision Job", job).get("state")
