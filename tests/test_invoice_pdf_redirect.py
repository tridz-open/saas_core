"""invoices.pdf must only redirect to allowlisted https gateway hosts (no open redirect)."""
import pytest

from test_api_billing import env, seed  # noqa: F401  (fixture reuse)


def _pdf(frappe, url, name="INV-X"):
    seed(frappe, "Invoice", name=name, tenant="acme", owner_user="a@x.io", status="open", total_amount=1,
         currency="USD", pdf_url=url, creation=9)
    from saas_core.api.v1 import invoices
    frappe.local.response = frappe._dict() if hasattr(frappe, "_dict") else {}
    return invoices.pdf(name)


@pytest.mark.parametrize("url", ["https://rzp.io/i/abc", "https://pages.razorpay.com/x",
                                 "https://invoice.stripe.com/i/acct/inv", "https://RZP.io/i/abc"])
def test_allowed_host_redirects(env, url):
    frappe, _ = env
    assert _pdf(frappe, url) is None
    assert frappe.local.response["type"] == "redirect" and frappe.local.response["location"] == url


@pytest.mark.parametrize("url", [
    "https://evil.io/x",
    "http://rzp.io/i/abc",
    "https://razorpay.com.evil.io/x",
    "https://evil.io/razorpay.com",
    "https://razorpay.com@evil.io/x",
    "https://evil.io@razorpay.com.evil.io/x",
    "https://rzp.io:443@evil.io/",
    "https://rzp.io\\@evil.io/",
    "//evil.io/x",
    "/files/../../etc/passwd",
    "javascript:alert(1)",
])
def test_disallowed_returns_not_available(env, url):
    frappe, _ = env
    out = _pdf(frappe, url)
    assert out["error"]["code"] == "not_available"
    assert frappe.local.response.get("type") != "redirect" and "location" not in frappe.local.response


def test_site_config_extends_allowlist(env):
    frappe, _ = env
    assert _pdf(frappe, "https://pay.example.com/i/1", "INV-A")["error"]["code"] == "not_available"
    frappe.conf["saas_core_pdf_url_hosts"] = "pay.example.com, other.io"
    assert _pdf(frappe, "https://pay.example.com/i/1", "INV-B") is None
    assert frappe.local.response["location"] == "https://pay.example.com/i/1"


def test_relative_file_path_served_when_file_exists(env):
    frappe, _ = env
    seed(frappe, "File", name="F-1", file_url="/private/files/inv1.pdf")
    frappe._store.docs[("File", "F-1")].get_content = lambda: b"%PDF"
    assert _pdf(frappe, "/private/files/inv1.pdf", "INV-F") is None
    assert frappe.local.response["type"] == "download" and frappe.local.response["filecontent"] == b"%PDF"


def test_relative_file_path_without_file_record(env):
    frappe, _ = env
    assert _pdf(frappe, "/files/nope.pdf", "INV-G")["error"]["code"] == "not_available"
