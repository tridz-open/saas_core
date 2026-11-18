from prov_helpers import seed, job


def test_setup_link_passed_to_welcome_job_and_password_not_stored(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    seed(frappe); j = job(frappe); port = FakeFcctl()
    assert runner.run_job(j, port=port) == "succeeded"
    t = frappe.get_doc("Tenant", "acme")
    assert not t.get("admin_password")
    assert port.sites["acme.apps.test"].get("admin_password_set")
    assert port.count("force_password_reset") == 0
    calls = [kw for name, kw in frappe._store.enqueued if name == "saas_core.notifications.welcome.send_tenant_setup"]
    assert len(calls) == 1
    kw = calls[0]
    assert kw["tenant"] == "acme" and kw["enqueue_after_commit"] is True
    assert "setup_url" not in kw  # P2-1: link is minted inside the job, never a job arg
    assert not any("update-password?key=" in repr(v) for v in kw.values())
    assert port.count("create_password_setup_link") == 0
    key = "update-password"
    assert key not in repr(t)
    assert not any(key in str(m) for m in frappe._store.messages)
    assert not any(key in str(x) for x in frappe._store.realtime)
    assert key not in repr(port.calls if hasattr(port, "calls") else "")


def test_port_without_setup_link_support_is_ignored(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl

    class Bare(FakeFcctl):
        def __getattribute__(self, n):
            if n in ("set_admin_password", "create_password_setup_link"):
                raise AttributeError(n)
            return super().__getattribute__(n)
    seed(frappe); j = job(frappe)
    assert runner.run_job(j, port=Bare()) == "succeeded"
    assert not frappe.get_doc("Tenant", "acme").get("admin_password")
    assert not frappe._store.enqueued


def test_rerun_does_not_reset_password_after_setup_mail_sent(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    seed(frappe); j = job(frappe); port = FakeFcctl()
    frappe._store.docs[("Tenant", "acme")]["setup_email_sent_at"] = "2026-01-01"
    assert runner.run_job(j, port=port) == "succeeded"
    assert port.count("set_admin_password") == 0
    assert not frappe._store.enqueued


def test_setup_secret_generated_stored_and_written_to_site_config(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    frappe.conf["saas_core_cp_url"] = "https://cp.example.com/"
    seed(frappe); j = job(frappe); port = FakeFcctl()
    assert runner.run_job(j, port=port) == "succeeded"
    t = frappe.get_doc("Tenant", "acme")
    secret = t.get("setup_secret")
    assert secret and len(secret) >= 32
    cfg = port.sites["acme.apps.test"]["site_config"]
    assert cfg == {"saas_setup_cp_url": "https://cp.example.com", "saas_setup_cp_secret": secret}
    assert port.count("set_site_config") == 1
    # the secret never lands in the call log or the audit/messages
    assert secret not in repr(port.calls) and secret not in repr(frappe._store.messages)
    # config is written before the setup mail job is queued
    assert [n for n, _ in frappe._store.enqueued] == ["saas_core.notifications.welcome.send_tenant_setup"]


def test_site_config_failure_skips_mail_and_does_not_reset_password(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl

    class Boom(FakeFcctl):
        def set_site_config(self, site, config):
            raise RuntimeError("pilot said no secret=abc")
    seed(frappe); j = job(frappe); port = Boom()
    assert runner.run_job(j, port=port) == "succeeded"
    assert port.count("set_admin_password") == 0 and not frappe._store.enqueued
    assert not any("abc" in repr(m) for m in frappe._store.messages)


def test_secret_reused_on_rerun(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    seed(frappe); j = job(frappe); port = FakeFcctl()
    t = frappe.get_doc("Tenant", "acme")
    t.get_password = lambda f, raise_exception=True: "already-minted-secret"
    frappe._store.docs[("Tenant", "acme")] = t
    runner.run_job(j, port=port)
    assert port.sites["acme.apps.test"]["site_config"]["saas_setup_cp_secret"] == "already-minted-secret"
