"""Every non-public v1 endpoint must be wrapped by the Guest guard (structural, via source introspection)."""
import ast, pathlib

ROOT = pathlib.Path(__file__).parent.parent / "saas_core" / "api" / "v1"
PUBLIC = {("plans", "list"), ("templates", "list"), ("templates", "get"), ("password_reset", "request"),
          ("password_reset", "confirm"), ("auth", "signup"), ("auth", "login"), ("tenants", "setup_complete")}


def _decorators(fn):
    out = []
    for d in fn.decorator_list:
        call = d if isinstance(d, ast.Call) else None
        target = (call.func if call else d)
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        guest = bool(call) and any(k.arg == "allow_guest" and getattr(k.value, "value", False) for k in call.keywords)
        out.append((name, guest))
    return out


def test_every_customer_endpoint_is_guest_guarded():
    seen = 0
    for path in ROOT.rglob("*.py"):
        if path.name in ("_common.py", "serializers.py"):
            continue
        mod = path.stem if path.name != "__init__.py" else path.parent.name
        for fn in ast.parse(path.read_text()).body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            decs = _decorators(fn)
            names = [n for n, _ in decs]
            if "whitelist" in names:
                raise AssertionError(f"{path.name}:{fn.name} uses bare frappe.whitelist; use authenticated/endpoint")
            if not ({"authenticated", "endpoint"} & set(names)):
                continue
            seen += 1
            guest = any(g for _, g in decs)
            if guest:
                assert (mod, fn.name) in PUBLIC or mod in ("auth", "verify_email", "checkout"), f"unexpected public {mod}.{fn.name}"
    assert seen > 40


def test_guard_raises_for_guest(frappe):
    from saas_core.api.v1 import _common
    import pytest
    frappe.session.user = "Guest"
    f = _common.guest_guard(lambda: 1)
    with pytest.raises(frappe.PermissionError):
        f()
    frappe.session.user = "a@x.io"
    assert f() == 1
