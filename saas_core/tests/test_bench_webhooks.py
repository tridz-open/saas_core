"""Webhook ingestion with a Razorpay Gateway Account holding a test secret (bench only)."""
import hashlib
import hmac
import json

import frappe
from frappe.tests.utils import FrappeTestCase

SECRET = "bench-webhook-secret"
ACCOUNT = "bench-razorpay"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class BenchWebhooks(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        if not frappe.db.exists("Gateway Account", ACCOUNT):
            frappe.get_doc({"doctype": "Gateway Account", "account_name": ACCOUNT, "gateway": "razorpay",
                            "mode": "test", "enabled": 1, "key_id": "rzp_test_x", "key_secret": "x",
                            "webhook_secret": SECRET}).insert(ignore_permissions=True)
        self._enq = frappe.enqueue
        self.enqueued = []
        frappe.enqueue = lambda *a, **k: self.enqueued.append((a, k))

    def tearDown(self):
        frappe.enqueue = self._enq

    def _body(self, eid="evt_bench_1") -> bytes:
        return json.dumps({"id": eid, "event": "payment.captured",
                           "payload": {"payment": {"entity": {"id": "pay_1", "amount": 100, "currency": "INR"}}}}).encode()

    def test_signed_event_accepted_then_duplicate(self):
        from saas_core import webhooks
        body = self._body()
        hdr = {"X-Razorpay-Signature": _sign(body), "X-Razorpay-Event-Id": "evt_bench_1"}
        r1 = webhooks.ingest("razorpay", hdr, body)
        self.assertIn(r1["status"], ("accepted", "ignored"))
        self.assertTrue(frappe.db.exists("Payment Event", "razorpay:evt_bench_1"))
        n = len(self.enqueued)
        r2 = webhooks.ingest("razorpay", hdr, body)
        self.assertEqual(r2["status"], "duplicate")
        self.assertEqual(len(self.enqueued), n)  # idempotent: nothing re-enqueued
        self.assertEqual(frappe.db.count("Payment Event", {"event_key": "razorpay:evt_bench_1"}), 1)

    def test_bad_signature_rejected(self):
        from saas_core import webhooks
        body = self._body("evt_bench_bad")
        with self.assertRaises(frappe.AuthenticationError):
            webhooks.ingest("razorpay", {"X-Razorpay-Signature": "deadbeef"}, body)
        self.assertFalse(frappe.db.exists("Payment Event", "razorpay:evt_bench_bad"))
