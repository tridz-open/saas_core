"""DEV ONLY: FcctlPort that provisions REAL Frappe sites on the same bench as the control plane via the bench CLI.

Selected when site_config `saas_core_local_bench=1` (see runner.get_port). Sites are `<subdomain>.localhost`.
Passwords go on argv (unavoidable with the bench CLI) but are redacted from every message and never logged.
"""
from __future__ import annotations

import glob
import os
import re
import secrets
import shutil
import subprocess
from typing import Any

from .ports import ProvisionSpec, RunResult

NEW_SITE_TIMEOUT = 1800
CMD_TIMEOUT = 600
_KEY_RE = re.compile(r"\b[0-9a-f]{56}\b")


class BenchError(RuntimeError):
    pass


def _mint_reset_key(user: str = "Administrator") -> str:
    """Runs INSIDE the target site (`bench --site X execute`). Same storage as fcctl siteadmin:
    reset_password_key = sha256(key), last_reset_password_key_generated_on = naive now in the site tz."""
    import hashlib

    import frappe
    from frappe.utils import now_datetime

    key = secrets.token_hex(28)
    frappe.db.set_value("User", user, {
        "reset_password_key": hashlib.sha256(key.encode()).hexdigest(),
        "last_reset_password_key_generated_on": now_datetime(),
    })
    frappe.db.commit()
    return key


def _conf(key: str, default=None):
    from saas_core.config import get_setting
    return get_setting(key, default)


class LocalBenchPort:
    UNVERIFIED = False

    def __init__(self, runner: Any = None, bench_path: str | None = None):
        self._run_fn = runner or subprocess.run
        self._bench_path = bench_path

    # ---- plumbing
    def bench_path(self) -> str:
        if not self._bench_path:
            import frappe.utils
            self._bench_path = frappe.utils.get_bench_path()
        return self._bench_path

    def _sites_dir(self) -> str:
        return os.path.join(self.bench_path(), "sites")

    def _bin(self) -> str:
        return _conf("local_bench_bin", None) or shutil.which("bench") or "bench"

    def _db_root_pw(self) -> str:
        pw = _conf("local_db_root_password", None)
        if not pw:
            raise BenchError("site_config saas_core_local_db_root_password is not set")
        return str(pw)

    def _port(self) -> str:
        return str(_conf("local_tenant_port", None) or 8004)

    def _bench(self, args: list[str], secrets_: tuple[str, ...] = (), timeout: int = CMD_TIMEOUT, check: bool = True):
        cmd = [self._bin(), *args]
        try:
            p = self._run_fn(cmd, cwd=self.bench_path(), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise BenchError(f"bench {args[0] if args[0] != '--site' else args[2]} timed out after {timeout}s") from e
        out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        if check and p.returncode != 0:
            raise BenchError(self._redact(f"bench failed ({p.returncode}): {out[-1500:]}", secrets_))
        p.redacted_output = self._redact(out, secrets_)
        return p

    @staticmethod
    def _redact(text: str, secrets_: tuple[str, ...]) -> str:
        for s in secrets_:
            if s:
                text = text.replace(s, "***")
        return text

    def _site_cmd(self, site: str, *args: str, secrets_: tuple[str, ...] = (), timeout: int = CMD_TIMEOUT):
        return self._bench(["--site", site, *args], secrets_, timeout)

    # ---- provisioning
    def find_workflow(self, workflow_id):
        return None

    # no trust_tier(): preflight skips the capacity check when the port lacks it (always allowed)

    def _extract_files(self, site: str, tarball: str) -> None:
        dest = os.path.join(self._sites_dir(), site)
        self._run_checked(["tar", "-xf", tarball, "-C", dest, "--strip-components=1"])

    def _run_checked(self, cmd: list[str]):
        p = self._run_fn(cmd, cwd=self.bench_path(), capture_output=True, text=True, timeout=CMD_TIMEOUT)
        if p.returncode != 0:
            raise BenchError(f"{cmd[0]} failed ({p.returncode}): {((p.stderr or '') + (p.stdout or ''))[-500:]}")

    def provision_site(self, spec: ProvisionSpec) -> RunResult:
        site = spec.site if "." in spec.site else f"{spec.site}.localhost"
        try:
            sql = _conf("local_golden_sql", None)
            pub = _conf("local_golden_files", None)
            priv = _conf("local_golden_private_files", None)
            root_pw = self._db_root_pw()
            admin_pw = secrets.token_urlsafe(18)
            sec = (root_pw, admin_pw)
            exists = os.path.exists(os.path.join(self._sites_dir(), site, "site_config.json"))
            if not exists:
                base = ["new-site", site, "--db-root-password", root_pw, "--admin-password", admin_pw]
                if sql:
                    p = self._bench([*base, "--source-sql", str(sql)], sec, NEW_SITE_TIMEOUT, check=False)
                    if p.returncode != 0 and re.search(r"no such option|unrecognized|unknown option", p.redacted_output, re.I):
                        self._bench(base, sec, NEW_SITE_TIMEOUT)
                        args = ["restore", str(sql), "--db-root-password", root_pw]
                        if pub:
                            args += ["--with-public-files", str(pub)]
                        if priv:
                            args += ["--with-private-files", str(priv)]
                        self._site_cmd(site, *args, secrets_=sec, timeout=NEW_SITE_TIMEOUT)
                        pub = priv = None
                    elif p.returncode != 0:
                        raise BenchError(f"bench failed ({p.returncode}): {p.redacted_output[-1500:]}")
                else:
                    self._bench(base, sec, NEW_SITE_TIMEOUT)
                for tarball in (pub, priv):
                    if tarball:
                        self._extract_files(site, str(tarball))
            self._site_cmd(site, "migrate", timeout=NEW_SITE_TIMEOUT)
            # recipe/template site_config (the FC port passes it on restore; do the same here)
            self.set_site_config(site, {k: v for k, v in (spec.config or {}).items() if isinstance(v, (str, int, float))})
        except (BenchError, OSError) as e:
            return RunResult(ok=False, state="failed", site=site, message=str(e)[:1500])
        return RunResult(ok=True, state="succeeded", site=site, handle=f"local:{site}", message="local bench site ready")

    def set_admin_password(self, site, password):
        self._site_cmd(site, "set-admin-password", password, secrets_=(password,))

    def force_password_reset(self, site, user="Administrator"):
        self.set_admin_password(site, secrets.token_urlsafe(18)) if user == "Administrator" else None

    def create_password_setup_link(self, site, user="Administrator") -> str:
        # `bench execute` only resolves methods of apps installed on that site, and saas_core is (correctly)
        # never installed on tenants, so run the helper through the site's console instead.
        script = ("from saas_core.provisioning.local_bench import _mint_reset_key\n"
                  f"print('KEY=' + _mint_reset_key({user!r}))\n")
        p = self._run_fn([self._bin(), "--site", site, "console"], cwd=self.bench_path(),
                         capture_output=True, text=True, timeout=CMD_TIMEOUT, input=script)
        m = _KEY_RE.search(p.stdout or "")
        if not m:
            raise BenchError("could not read reset key from bench execute output")
        return f"http://{site}:{self._port()}/update-password?key={m.group(0)}"

    def set_site_config(self, site, config):
        for k, v in (config or {}).items():
            self._site_cmd(site, "set-config", str(k), str(v), secrets_=(str(v),))

    def login_link(self, site):
        return f"http://{site}:{self._port()}/login"

    # ---- lifecycle
    def _maint(self, site, on: bool, what: str) -> RunResult:
        try:
            self._site_cmd(site, "set-maintenance-mode", "on" if on else "off")
        except BenchError as e:
            return RunResult(ok=False, state="failed", site=site, message=str(e))
        return RunResult(ok=True, site=site, handle=f"local:{what}:{site}")

    def suspend_site(self, site, *, workflow_id=None):
        return self._maint(site, True, "suspend")

    def resume_site(self, site, *, workflow_id=None):
        return self._maint(site, False, "resume")

    def terminate_site(self, site, *, workflow_id=None):
        try:
            pw = self._db_root_pw()
            self._bench(["drop-site", site, "--force", "--no-backup", "--db-root-password", pw], (pw,))
        except BenchError as e:
            return RunResult(ok=False, state="failed", site=site, message=str(e))
        return RunResult(ok=True, site=site, handle=f"local:terminate:{site}")

    def _backup_dir(self, site):
        return os.path.join(self._sites_dir(), site, "private", "backups")

    def create_backup(self, site, *, workflow_id=None):
        try:
            self._site_cmd(site, "backup", "--with-files")
        except BenchError as e:
            return RunResult(ok=False, state="failed", site=site, message=str(e))
        items = self.list_backups(site)
        return RunResult(ok=True, site=site, handle=items[0]["id"] if items else None)

    def list_backups(self, site):
        files = sorted(glob.glob(os.path.join(self._backup_dir(site), "*-database.sql.gz")), reverse=True)
        return [{"id": os.path.basename(f).removesuffix("-database.sql.gz"), "path": f} for f in files]

    def restore_backup(self, site, backup, *, workflow_id=None):
        path = os.path.join(self._backup_dir(site), f"{backup}-database.sql.gz")
        try:
            pw = self._db_root_pw()
            self._site_cmd(site, "restore", path, "--force", "--db-root-password", pw, secrets_=(pw,), timeout=NEW_SITE_TIMEOUT)
        except BenchError as e:
            return RunResult(ok=False, state="failed", site=site, message=str(e))
        return RunResult(ok=True, site=site, handle=f"local:restore:{backup}")

    def backup_download_link(self, site, backup_id):
        raise NotImplementedError("local bench mode has no backup download links")

    def change_plan(self, site, plan, *, workflow_id=None):
        return RunResult(ok=True, site=site, handle=f"local:change_plan:{site}", message="no-op in local bench mode")
