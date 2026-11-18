import json, pathlib, types

P = pathlib.Path(__file__).parent.parent / "saas_core/saas_core/doctype/platform_settings/platform_settings.json"


def test_json():
    d = json.loads(P.read_text())
    assert d["issingle"] == 1
    assert [f["fieldname"] for f in d["fields"]] == "session_key public_signup default_gateway site_domain payment_banner capacity_open signup_mode signup_allow".split()
    assert d["fields"][0]["fieldtype"] == "Password"
    assert {p["role"] for p in d["permissions"]} == {"SaaS Admin", "System Manager"}
    assert [f for f in d["fields"] if f["fieldname"] == "payment_banner"][0]["default"] == "Test mode: payments mocked"


def test_fallback_order(monkeypatch, frappe):
    import importlib; config = importlib.import_module("saas_core.config")
    frappe.conf.pop("saas_core_site_domain", None)
    assert config.get_setting("site_domain", "d") == "d"
    monkeypatch.setattr(frappe.db, "get_single_value", lambda dt, k: "settings.example", raising=False)
    assert config.get_setting("site_domain", "d") == "settings.example"
    frappe.conf["saas_core_site_domain"] = "conf.example"
    try:
        assert config.get_setting("site_domain", "d") == "conf.example"
    finally:
        frappe.conf.pop("saas_core_site_domain")


def test_builtin_default(frappe):
    import importlib; config = importlib.import_module("saas_core.config")
    assert config.get_setting("payment_banner") == "Test mode: payments mocked"


def test_session_key_install(monkeypatch, frappe):
    import importlib; install = importlib.import_module("saas_core.install")
    class D(dict):
        def get_password(s, k, raise_exception=True): return s.get(k)
        def save(s, **kw): s["saved"] = True
        def __getattr__(s, k): return s.get(k)
        def __setattr__(s, k, v): s[k] = v
    doc = D()
    monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: doc)
    install.ensure_session_key()
    key = doc["session_key"]
    from cryptography.fernet import Fernet
    Fernet(key.encode())
    install.ensure_session_key()
    assert doc["session_key"] == key
