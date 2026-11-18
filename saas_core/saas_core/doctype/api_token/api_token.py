"""API Token controller. Only sha256(secret) is stored; the secret is never persisted."""
from frappe.model.document import Document


class APIToken(Document):
    pass
