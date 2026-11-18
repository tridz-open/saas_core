"""Ports (Protocols) and shared value types for provisioning. No frappe or fcctl imports here."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, ContextManager, Generic, Protocol, TypeVar, runtime_checkable

APPROVALS_REQUIRED = 2
APPROVAL_TTL_MINUTES = 30

T = TypeVar("T")

JOB_STATES = ("pending", "dispatching", "submitted", "running", "succeeded", "failed",
              "stuck", "outcome_unknown", "needs_attention")
TERMINAL_STATES = ("succeeded", "failed")
ACTIVE_STATES = ("dispatching", "submitted", "running", "stuck", "outcome_unknown")

_TRANSITIONS = {
    "pending": {"dispatching", "failed"},
    "dispatching": {"submitted", "outcome_unknown", "failed", "succeeded", "running", "pending"},
    "submitted": {"running", "succeeded", "failed", "stuck", "dispatching", "pending"},
    "running": {"succeeded", "failed", "stuck", "dispatching", "pending"},
    "stuck": {"running", "succeeded", "failed", "dispatching", "pending"},
    "outcome_unknown": {"dispatching", "submitted", "running", "succeeded", "failed", "pending"},
    "needs_attention": {"pending", "dispatching", "succeeded", "failed"},
    "succeeded": set(),
    "failed": {"pending"},
}


class InvalidTransition(Exception):
    pass


def check_transition(old: str, new: str) -> None:
    """Journal state machine (SDK_PLAN_V2 7.2). Same-state is a no-op; any -> needs_attention is always legal."""
    if old == new or new == "needs_attention":
        return
    if new not in _TRANSITIONS.get(old, set()):
        raise InvalidTransition(f"{old} -> {new}")


class CasConflict(Exception):
    """Session save lost an optimistic-concurrency race."""


class LockBusy(Exception):
    pass


class ApprovalError(Exception):
    pass


@dataclass
class Versioned(Generic[T]):
    value: T
    version: int


@dataclass
class RunResult:
    ok: bool
    state: str = "succeeded"            # succeeded | running | failed
    site: str | None = None
    handle: str | None = None
    message: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class ProvisionSpec:
    workflow_id: str
    site: str
    tenant: str
    plan: str | None = None             # fc server plan
    region: str | None = None
    manifest: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    hooks: list = field(default_factory=list)
    domain: str | None = None           # None: `site` is a bare subdomain; the port picks FC's default domain


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_digest(op: str, target: str, args: dict | None) -> str:
    return hashlib.sha256(canonical_json({"op": op, "target": target, "args": args or {}}).encode()).hexdigest()


@runtime_checkable
class SessionStore(Protocol):
    def load(self, login: str) -> Versioned[dict] | None: ...
    def save(self, login: str, session: dict, expected_version: int) -> int: ...
    def lock(self, login: str, ttl: float) -> ContextManager: ...


class Journal(Protocol):
    def begin(self, job: str, name: str, *, hook_version: str | None = None) -> int: ...
    def transition(self, job: str, seq: int, state: str, *, message: str | None = None, handle: str | None = None) -> None: ...
    def steps(self, job: str) -> list[dict]: ...
    def set_job_state(self, job: str, state: str, *, error: str | None = None) -> None: ...


class FcctlPort(Protocol):
    def find_workflow(self, workflow_id: str) -> RunResult | None: ...
    def provision_site(self, spec: ProvisionSpec) -> RunResult: ...
    def suspend_site(self, site: str, *, workflow_id: str | None = None) -> RunResult: ...
    def resume_site(self, site: str, *, workflow_id: str | None = None) -> RunResult: ...
    def terminate_site(self, site: str, *, workflow_id: str | None = None) -> RunResult: ...
    def create_backup(self, site: str, *, workflow_id: str | None = None) -> RunResult: ...
    def restore_backup(self, site: str, backup: str, *, workflow_id: str | None = None) -> RunResult: ...
    def list_backups(self, site: str) -> list[dict]: ...
    def backup_download_link(self, site: str, backup_id: str) -> str: ...
    def login_link(self, site: str) -> str: ...
    def set_admin_password(self, site: str, password: str) -> None: ...
    def force_password_reset(self, site: str, user: str = "Administrator") -> None: ...
    def create_password_setup_link(self, site: str, user: str = "Administrator") -> str: ...
    def set_site_config(self, site: str, config: dict) -> None: ...
    def change_plan(self, site: str, plan: str, *, workflow_id: str | None = None) -> RunResult: ...
