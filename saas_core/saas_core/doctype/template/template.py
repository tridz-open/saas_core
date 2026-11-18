"""Template controller."""
import frappe
from frappe.model.document import Document


class Template(Document):
    def validate(self):
        from saas_core.provisioning.templates import TemplateError, validate_template
        try:
            warnings = validate_template(self).get("warnings") or []
        except TemplateError as e:
            frappe.throw(str(e))
        for w in warnings:
            frappe.msgprint(w, indicator="orange", title="Template warning")
