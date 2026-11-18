"""FC Connect desk page backend (SaaS Admin only). Thin: logic lives in saas_core.jobs."""
from __future__ import annotations

import importlib

import frappe

ROLES = ("SaaS Admin", "System Manager")


def _guard():
    if not set(ROLES) & set(frappe.get_roles()):
        frappe.throw("Not permitted", frappe.PermissionError)


def _jobs():
    return importlib.import_module("saas_core.jobs")


@frappe.whitelist()
def send_code(email):
    _guard()
    return _jobs().fc_send_code(email)


@frappe.whitelist()
def verify(code, email=None):
    _guard()
    email = email or frappe.conf.get("saas_core_fc_email")
    if not email:
        frappe.throw("email required")
    return _jobs().fc_verify(email, str(code).strip())


@frappe.whitelist()
def status():
    _guard()
    return _jobs().fc_status()
