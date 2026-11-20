import pytest


def _install(frappe):
    from saas_core import install
    return install


def test_blocks_on_saas_golden(frappe):
    frappe.conf["saas_golden"] = 1
    with pytest.raises(frappe.ValidationError, match="template site"):
        _install(frappe).before_install()


def test_allows_normal_site(frappe):
    assert _install(frappe).before_install() is None


def test_hook_registered():
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "saas_core" / "hooks.py").read_text()
    assert 'before_install = "saas_core.install.before_install"' in src
