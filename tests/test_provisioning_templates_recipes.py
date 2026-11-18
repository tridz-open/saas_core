import json
import pytest


def test_template_manifest_ok_and_invalid(frappe):
    from saas_core.provisioning.templates import validate_template, TemplateError
    doc = {"slug": "t", "recipe": "r", "apps": [{"app": "b", "install_order": 2}, {"app": "a", "install_order": 1}],
           "hooks": json.dumps([{"name": "h", "version": 1}]), "backup_source": json.dumps({"profile": "p", "site": "s"})}
    m = validate_template(doc)
    assert [a["app"] for a in m["apps"]] == ["a", "b"] and m["hooks"][0]["name"] == "h"
    for bad in ({"slug": "t", "apps": []}, {**doc, "hooks": json.dumps([{"name": "h"}])},
                {**doc, "apps": [{"app": "a"}, {"app": "a"}]}, {**doc, "backup_source": json.dumps({"x": 1})},
                {**doc, "site_config": "not json"}):
        with pytest.raises(TemplateError):
            validate_template(bad)


def test_recipe_registry(frappe):
    from saas_core.recipes import registry
    from saas_core.recipes.base import Recipe
    class A(Recipe): slug = "a"
    r = registry.register(A())
    assert registry.get("a") is r and "a" in registry.all() and registry.get("nope") is None
    assert r.tenant_config(None, None, {}) == {} and r.hooks() == {} and r.signup_schema() == {}
    with pytest.raises(ValueError):
        registry.register(Recipe())
    registry.unregister("a")
    assert registry.get("a") is None


def test_recipe_entry_points(frappe, monkeypatch):
    from saas_core.recipes import registry
    from saas_core.recipes.base import Recipe
    class E(Recipe): slug = "ep"
    class EP:
        name = "ep"
        def load(self): return E
    class EPS:
        def select(self, group): return [EP()] if group == "saas_core.recipes" else []
    monkeypatch.setattr(registry, "entry_points", lambda: EPS())
    registry.reload()
    assert isinstance(registry.get("ep"), E)
