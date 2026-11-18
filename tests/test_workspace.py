"""Validate SaaS Admin workspace, number card and sidebar JSON files."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "saas_core" / "saas_core"
DOCTYPES = {json.loads(p.read_text())["name"] for p in (ROOT / "doctype").glob("*/*.json")}


def _files():
    return [p for d in ("workspace", "number_card", "workspace_sidebar") for p in (ROOT / d).glob("*/*.json")]


def test_json_valid():
    fs = _files()
    assert len(fs) == 5
    for p in fs:
        text = p.read_text()
        json.loads(text)


def test_workspace_shape():
    ws = json.loads((ROOT / "workspace/saas_admin/saas_admin.json").read_text())
    content = json.loads(ws["content"])
    types = {b["type"] for b in content}
    assert {"header", "number_card", "shortcut"} <= types
    assert ws["roles"] == [{"role": "SaaS Admin"}]
    cards = {c["number_card_name"] for c in ws["number_cards"]}
    assert {b["data"]["number_card_name"] for b in content if b["type"] == "number_card"} == cards
    labels = {s["label"] for s in ws["shortcuts"]}
    assert {b["data"]["shortcut_name"] for b in content if b["type"] == "shortcut"} == labels
    assert len(labels) == 10
    for s in ws["shortcuts"]:
        assert s["link_to"] in DOCTYPES


def test_cards_reference_doctypes():
    for p in (ROOT / "number_card").glob("*/*.json"):
        d = json.loads(p.read_text())
        assert d["document_type"] in DOCTYPES
        assert isinstance(json.loads(d["filters_json"]), list)
