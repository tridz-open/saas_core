"""The skeleton and conformance test in docs/gateways/ADDING_A_GATEWAY.md must really run and pass the kit."""
import re
import sys
import types
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "gateways" / "ADDING_A_GATEWAY.md"


def _block(marker):
    text = DOC.read_text()
    m = re.search(re.escape(marker) + r"\s*```python\n(.*?)```", text, re.S)
    assert m, f"{marker} block missing"
    return m.group(1)


def test_documented_skeleton_passes_conformance(frappe, monkeypatch):
    pkg = types.ModuleType("my_app")
    pkg.__path__ = []
    gws = types.ModuleType("my_app.gateways")
    gws.__path__ = []
    mod = types.ModuleType("my_app.gateways.mygw")
    exec(compile(_block("<!-- skeleton -->"), "skeleton", "exec"), mod.__dict__)
    monkeypatch.setitem(sys.modules, "my_app", pkg)
    monkeypatch.setitem(sys.modules, "my_app.gateways", gws)
    monkeypatch.setitem(sys.modules, "my_app.gateways.mygw", mod)
    ns = {"__name__": "doc_test"}
    exec(compile(_block("<!-- conformance-test -->"), "conformance", "exec"), ns)
    t = ns["TestMyGwConformance"]()
    ran = skipped = 0
    import pytest
    for name in dir(t):
        if name.startswith("test_"):
            try:
                getattr(t, name)()
                ran += 1
            except pytest.skip.Exception:
                skipped += 1
    assert ran >= 15 and skipped <= 3, (ran, skipped)


def test_documented_hook_validates(frappe, monkeypatch):
    from saas_core.gateways import registry
    mod = types.ModuleType("my_app.gateways.mygw")
    exec(compile(_block("<!-- skeleton -->"), "skeleton", "exec"), mod.__dict__)
    frappe.conf["_test_hooks"] = {"saas_core_gateways": {"mygw": "my_app.gateways.mygw.MyGwGateway"}}
    frappe.get_attr = lambda path: mod.MyGwGateway
    assert registry.gateway_class("mygw") is mod.MyGwGateway
