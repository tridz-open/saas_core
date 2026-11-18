"""Deterministic in-memory FcctlPort with failure injection and a call log. Never touches the network."""
from __future__ import annotations

import dataclasses

from .ports import ProvisionSpec, RunResult

PROVISION_STEPS = ["create_site", "install_apps", "restore_backup", "post_restore_hooks", "health_checks"]


class FakeCrash(BaseException):
    """Simulates a killed worker (BaseException so runner error handling cannot swallow it)."""


class FakeFcctl:
    def __init__(self, fail_at_step: str | None = None, crash_after: str | None = None):
        self.fail_at_step = fail_at_step      # a provision step or a method name -> returns ok=False
        self.crash_after = crash_after        # one-shot FakeCrash after this step/method completes its effect
        self.calls: list[dict] = []
        self.sites: dict[str, dict] = {}
        self.workflows: dict[str, dict] = {}  # wf -> {"state","done","site","message","handle"}
        self.backups: dict[str, list[dict]] = {}
        self.sites_created = 0
        self.tier: dict | None = None         # set to enable trust_tier()/preflight
        self.bucket: dict[tuple, bytes] = {}
        self.uploads: dict[str, dict] = {}
        self.restores: list[dict] = []

    # -- helpers
    def count(self, method: str) -> int:
        return sum(1 for c in self.calls if c["method"] == method)

    def _log(self, method: str, **args):
        self.calls.append({"method": method, "args": args})

    def _crash(self, label: str):
        if self.crash_after == label:
            self.crash_after = None
            raise FakeCrash(label)

    def _res(self, wf: dict) -> RunResult:
        return RunResult(ok=wf["state"] != "failed", state=wf["state"], site=wf["site"], handle=wf["handle"],
                         message=wf["message"], data={"done": list(wf["done"])})

    # -- port
    def find_workflow(self, workflow_id):
        self._log("find_workflow", workflow_id=workflow_id)
        wf = self.workflows.get(workflow_id)
        return self._res(wf) if wf else None

    default_domain = "fc.localhost"

    def provision_site(self, spec: ProvisionSpec) -> RunResult:
        if not spec.domain and "." not in spec.site:      # bare subdomain: FC's default domain
            spec = dataclasses.replace(spec, site=f"{spec.site}.{self.default_domain}")
        self._log("provision_site", workflow_id=spec.workflow_id, site=spec.site)
        wf = self.workflows.get(spec.workflow_id)
        if wf and wf["state"] == "succeeded":
            return self._res(wf)
        if not wf:
            if spec.site in self.sites:
                return RunResult(ok=False, state="failed", site=spec.site, message="site exists")
            wf = {"state": "running", "done": [], "site": spec.site, "message": "", "handle": f"task-{spec.workflow_id}"}
            self.workflows[spec.workflow_id] = wf
        wf["state"] = "running"
        for step in PROVISION_STEPS:
            if step in wf["done"]:
                continue
            if self.fail_at_step == step:
                wf.update(state="failed", message=f"failed at {step}")
                return self._res(wf)
            if step == "create_site":
                if spec.site not in self.sites:
                    self.sites[spec.site] = {"status": "active", "plan": spec.plan, "workflow_id": spec.workflow_id,
                                             "config": dict(spec.config), "manifest": dict(spec.manifest)}
                    self.sites_created += 1
            if step == "restore_backup":
                src = (spec.manifest or {}).get("backup_source")
                if isinstance(src, dict) and src.get("bucket"):   # restore-from-golden (C6)
                    from .backups import restore_bucket_source
                    r = restore_bucket_source(self, spec.site, src, workflow_id=spec.workflow_id)
                    if not r.ok:
                        wf.update(state="failed", message=r.message or "restore failed")
                        return self._res(wf)
            wf["done"].append(step)
            self._crash(step)
        wf["state"] = "succeeded"
        if self.fail_at_step == "provision_site":
            wf.update(state="failed", message="failed at provision_site")
            return self._res(wf)
        self._crash("provision_site")
        return self._res(wf)

    def _simple(self, method: str, site: str, workflow_id, new_status: str | None, **extra) -> RunResult:
        self._log(method, site=site, workflow_id=workflow_id, **extra)
        if self.fail_at_step == method:
            return RunResult(ok=False, state="failed", site=site, message=f"failed at {method}")
        s = self.sites.get(site)
        if s is None and new_status != "terminated":
            return RunResult(ok=False, state="failed", site=site, message="no such site")
        if s is not None and new_status:
            if new_status == "terminated":
                self.sites.pop(site)
            else:
                s["status"] = new_status
        self._crash(method)
        return RunResult(ok=True, state="succeeded", site=site, handle=f"{method}:{workflow_id}")

    def suspend_site(self, site, *, workflow_id=None):
        return self._simple("suspend_site", site, workflow_id, "suspended")

    def resume_site(self, site, *, workflow_id=None):
        return self._simple("resume_site", site, workflow_id, "active")

    def terminate_site(self, site, *, workflow_id=None):
        return self._simple("terminate_site", site, workflow_id, "terminated")

    def change_plan(self, site, plan, *, workflow_id=None):
        r = self._simple("change_plan", site, workflow_id, None, plan=plan)
        if r.ok:
            self.sites[site]["plan"] = plan
        return r

    def create_backup(self, site, *, workflow_id=None):
        self._log("create_backup", site=site, workflow_id=workflow_id)
        if self.fail_at_step == "create_backup":
            return RunResult(ok=False, state="failed", site=site, message="failed at create_backup")
        lst = self.backups.setdefault(site, [])
        found = next((b for b in lst if b["workflow_id"] == workflow_id and workflow_id), None)
        if not found:
            found = {"id": f"bk-{site}-{len(lst) + 1}", "workflow_id": workflow_id}
            lst.append(found)
        self._crash("create_backup")
        return RunResult(ok=True, site=site, handle=found["id"], data={"backup": found["id"]})

    def restore_backup(self, site, backup, *, workflow_id=None):
        return self._simple("restore_backup", site, workflow_id, None, backup=backup)

    def list_backups(self, site):
        self._log("list_backups", site=site)
        return [dict(b) for b in self.backups.get(site, [])]

    def backup_download_link(self, site, backup_id):
        self._log("backup_download_link", site=site, backup_id=backup_id)
        return f"https://fake.invalid/backups/{site}/{backup_id}"

    def login_link(self, site):
        self._log("login_link", site=site)
        return f"https://{site}/login?token=fake"

    def set_admin_password(self, site, password):
        self._log("set_admin_password", site=site, password="***")  # never record the secret
        self.sites.setdefault(site, {})["admin_password_set"] = True
        self._admin_passwords = getattr(self, "_admin_passwords", {})
        self._admin_passwords[site] = password

    def force_password_reset(self, site, user="Administrator"):
        self._log("force_password_reset", site=site, user=user)
        self.sites.setdefault(site, {}).setdefault("force_reset", set()).add(user)

    def create_password_setup_link(self, site, user="Administrator"):
        import secrets
        self._log("create_password_setup_link", site=site, user=user)  # never record the key
        return f"https://{site}/update-password?key={secrets.token_hex(28)}"

    def set_site_config(self, site, config):
        keys = sorted(config)
        self._log("set_site_config", site=site, keys=keys)  # keys only: values may be secrets
        self.sites.setdefault(site, {}).setdefault("site_config", {}).update(dict(config))

    # -- trust tier / bucket / upload-restore (C5, C6)
    def trust_tier(self):
        self._log("trust_tier")
        return dict(self.tier) if self.tier is not None else None

    def bucket_put(self, bucket, key, data: bytes):
        self._log("bucket_put", bucket=bucket, key=key)
        self.bucket[(bucket, key)] = data

    def bucket_get(self, bucket, key) -> bytes:
        self._log("bucket_get", bucket=bucket, key=key)
        if (bucket, key) not in self.bucket and key.endswith("manifest.json"):
            self._seed_golden(bucket, key)  # local dev: synthesize a golden backup on first use
        return self.bucket[(bucket, key)]

    def _seed_golden(self, bucket, manifest_key):
        import hashlib
        import json
        prefix = manifest_key.rsplit("/", 1)[0]
        blob = b"-- fake golden backup\n"
        name = "database.sql.gz"
        self.bucket[(bucket, f"{prefix}/{name}")] = blob
        manifest = {"parts": [{"name": name, "key": f"{prefix}/{name}", "sha256": hashlib.sha256(blob).hexdigest()}]}
        self.bucket[(bucket, manifest_key)] = json.dumps(manifest).encode()

    def bucket_list(self, bucket, prefix=""):
        return sorted(k for b, k in self.bucket if b == bucket and k.startswith(prefix))

    def bucket_delete(self, bucket, key):
        self._log("bucket_delete", bucket=bucket, key=key)
        self.bucket.pop((bucket, key), None)

    def copy_backup_to_bucket(self, site, backup_id, bucket, prefix):
        self._log("copy_backup_to_bucket", site=site, backup_id=backup_id, prefix=prefix)
        for part in ("database.sql.gz", "public.tar", "private.tar"):
            self.bucket[(bucket, prefix + part)] = f"{backup_id}:{part}".encode()

    def create_upload(self, site):
        self._log("create_upload", site=site)
        uid = f"up-{len(self.uploads) + 1}"
        self.uploads[uid] = {"site": site, "parts": {}}
        return uid

    def put_upload_part(self, upload_id, name, data: bytes):
        self._log("put_upload_part", upload_id=upload_id, name=name)
        self.uploads[upload_id]["parts"][name] = data

    def restore_from_upload(self, site, body, *, workflow_id=None):
        self._log("restore_from_upload", site=site, body=body)
        up = self.uploads.get(body["upload_id"])
        if not up or set(body["parts"]) - set(up["parts"]):
            return RunResult(ok=False, state="failed", site=site, message="missing upload parts")
        self.restores.append({"site": site, **body})
        return RunResult(ok=True, site=site, handle=f"restore_upload:{workflow_id}")
