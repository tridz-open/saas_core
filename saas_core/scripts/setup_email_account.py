"""Upsert the outgoing Email Account from SAAS_SMTP_* env (fallback: site conf saas_smtp_*).

Usage: bench --site <site> execute saas_core.scripts.setup_email_account.setup
"""
import os

import frappe

from saas_core.config import get_setting

KEYS = ("host", "port", "user", "password", "from")


def _cfg(key):
	val = os.environ.get(f"SAAS_SMTP_{key.upper()}")
	if val in (None, ""):
		val = frappe.conf.get(f"saas_smtp_{key}")
	return val if val not in (None, "") else None


def setup(email_id=None):
	cfg = {k: _cfg(k) for k in KEYS}
	email_id = email_id or cfg["from"] or cfg["user"]
	missing = [k for k in ("host", "port", "user", "password") if not cfg[k]]
	if missing or not email_id:
		frappe.throw(
			"Missing SMTP settings: " + ", ".join(missing + ([] if email_id else ["from"])),
			title="saas_core email setup",
		)
	port = int(cfg["port"])
	ssl = 1 if port == 465 else 0
	tls = 1 if port in (587, 25, 2525) else 0

	name = frappe.db.get_value("Email Account", {"email_id": email_id}, "name")
	doc = frappe.get_doc("Email Account", name) if name else frappe.new_doc("Email Account")
	doc.update(
		{
			"email_account_name": doc.get("email_account_name") or get_setting("brand", "") or "SaaS Outgoing",
			"email_id": email_id,
			"login_id": cfg["user"],
			"smtp_server": cfg["host"],
			"smtp_port": port,
			"use_tls": tls,
			"use_ssl_for_outgoing": ssl,
			"enable_outgoing": 1,
			"default_outgoing": 1,
			"awaiting_password": 0,
			"enable_incoming": doc.get("enable_incoming") or 0,
		}
	)
	doc.password = cfg["password"]
	doc.flags.ignore_permissions = True
	doc.save() if name else doc.insert()
	frappe.db.commit()
	return {
		"name": doc.name,
		"email_id": email_id,
		"smtp_server": cfg["host"],
		"smtp_port": port,
		"use_tls": tls,
		"use_ssl_for_outgoing": ssl,
		"enable_outgoing": 1,
		"default_outgoing": 1,
		"created": not name,
	}
