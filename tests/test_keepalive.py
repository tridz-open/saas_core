import importlib


def test_hooks_register_keepalive(frappe):
    import ast, pathlib
    src = pathlib.Path(__file__).parent.parent / "saas_core" / "hooks.py"
    assert "saas_core.jobs.keepalive" in src.read_text()


def test_keepalive_no_session(frappe):
    frappe.conf["saas_core_fake_fcctl"] = 1
    assert importlib.import_module("saas_core.jobs").keepalive()["reason"] == "no_session"


def test_daily_backups_guarded(frappe):
    j = importlib.import_module("saas_core.jobs")
    try:
        importlib.import_module("saas_core.provisioning.backups")
        return
    except ImportError:
        pass
    assert j.daily_tenant_backups() is None
