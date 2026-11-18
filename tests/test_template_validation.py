import pytest

FULL = [{"name": "core.scheduler_resume", "version": "1"}, {"name": "core.maintenance_off", "version": "1"}]


def tpl(hooks=None, **kw):
    d = {"slug": "t", "apps": [{"app": "erpnext"}], "hooks": FULL if hooks is None else hooks,
         "site_admin": 1, "is_public": 1}
    d.update(kw)
    return d


def run(doc):
    from saas_core.provisioning.templates import validate_template
    return validate_template(doc)


@pytest.mark.parametrize("name", ["core.set_host_name", "core.set_fresh_encryption_key"])
def test_protected_hook_rejected(frappe, name):
    from saas_core.provisioning.templates import TemplateError
    with pytest.raises(TemplateError, match="remove it; Pilot manages"):
        run(tpl(FULL + [{"name": name, "version": "1"}]))


def test_clean_template_has_no_warnings(frappe):
    assert run(tpl())["warnings"] == []


def test_missing_release_hooks_warn(frappe):
    w = run(tpl([]))["warnings"]
    assert len(w) == 1 and "fcctl releases isolation by default from v0.1.x" in w[0]
    assert "core.scheduler_resume" in w[0] and "core.maintenance_off" in w[0]
    assert len(run(tpl(FULL[:1]))["warnings"]) == 1


def test_recipe_hooks_without_site_admin_warn(frappe):
    h = FULL + [{"name": "acme.seed", "version": "1"}]
    assert any("site_admin is off" in x for x in run(tpl(h, site_admin=0))["warnings"])
    assert run(tpl(h, site_admin=1))["warnings"] == []


def test_private_template_on_public_plan_warns(frappe, monkeypatch):
    monkeypatch.setattr(frappe, "get_all", lambda *a, **k: [{"name": "p"}])
    assert any("is_public is 0" in x for x in run(tpl(is_public=0))["warnings"])
    assert run(tpl(is_public=1))["warnings"] == []


def test_private_template_without_public_plan_no_warning(frappe, monkeypatch):
    monkeypatch.setattr(frappe, "get_all", lambda *a, **k: [])
    assert run(tpl(is_public=0))["warnings"] == []


def test_doctype_validate_msgprints_warnings(frappe, monkeypatch):
    import importlib
    mod = importlib.import_module("saas_core.saas_core.doctype.template.template")

    class D(dict):
        __getattr__ = dict.get

    d = D(tpl([]))
    seen = []
    monkeypatch.setattr(frappe, "msgprint", lambda *a, **k: seen.append(a[0]))
    mod.Template.validate(d)
    assert seen and "scheduler_resume" in seen[0]


def test_fc_check_lists_template_warnings(frappe, monkeypatch):
    from saas_core.provisioning import fc_client
    monkeypatch.setattr(frappe, "get_all", lambda dt, **k: ["t"] if dt == "Template" else [])
    monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: tpl([]))
    w = fc_client._template_warnings()
    assert list(w) == ["t"] and "scheduler_resume" in w["t"][0]
