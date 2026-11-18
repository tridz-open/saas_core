"""The shared email layout: dark-mode safe (no band/blush fills), Outlook fallback, configurable footer contact."""
import json
import pathlib

FIX = pathlib.Path(__file__).resolve().parent.parent / "saas_core" / "fixtures" / "email_template.json"
DOCS = json.loads(FIX.read_text())


def test_all_saas_templates_use_shared_layout():
    assert len(DOCS) == 9
    for d in DOCS:
        h = d["response_html"]
        assert 'bgcolor="#F6F6F4"' in h and "logo-light" in h and "logo-dark" in h, d["name"]
        assert "v:roundrect" in h and "prefers-color-scheme:dark" in h and "[data-ogsc]" in h, d["name"]
        assert "{{ support_email }}" in h and "support@" not in h, d["name"]
        assert "border-radius:12px 12px 0 0" not in h and "FCF0F4" not in h and "background-color:{{ brand_color" not in h.split("<body")[1].split("<table")[0], d["name"]


def test_logo_dark_in_context(monkeypatch):
    from saas_core.notifications import templating as T
    monkeypatch.setattr(T, "get_setting", lambda k, d=None: "https://x.test/d.png" if k == "logo_url_dark" else d)
    assert T.brand_context()["logo_url_dark"] == "https://x.test/d.png"
