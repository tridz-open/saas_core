"""T1: Razorpay checkout end to end on a real bench (no network, FakeFcctl).

signup (open mode) -> checkout.create_session(gateway=razorpay) -> simulated browser callback signed with the
test key_secret -> checkout.verify_payment -> Payment Event processing -> verified callback -> Provision Job (fake fcctl; webhook adds no duplicate) -> tenant active + welcome mail with an /update-password?key= link and no password
-> replay of the same webhook creates nothing new.

Needs: bench migrate (doctypes + fixtures incl. Email Template saas-tenant-ready),
`bench --site <s> set-config allow_tests true`.
Run: bench --site <s> run-tests --app saas_core --module saas_core.tests.test_bench_e2e_razorpay
"""
import hashlib
import hmac
import json
from email import message_from_string

import frappe
from frappe.tests.utils import FrappeTestCase

P = "e2erzp"
ACCOUNT = "rzp-e2e"
KEY_ID = "rzp_test_E2E"
KEY_SECRET = "e2e-key-secret"
WEBHOOK_SECRET = "e2e-webhook-secret"
SLUG = f"{P}-acme"
EMAIL = f"{P}-owner@cp.test"
PASSWORD = "Str0ng-pass-9999"
SUB_ID = "sub_E2E0001"
PAY_ID = "pay_E2E0001"
EVT_ID = "evt_E2E_charged_1"
PLAN = f"{P}-standard"
TPL = f"{P}-acme"
URLS = {"success_url": "https://cp.test/ok", "cancel_url": "https://cp.test/no"}
_ENQ_OPTS = ("queue", "timeout", "enqueue_after_commit", "now", "is_async", "at_front", "job_name", "job_id")


def _hmac(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


class FakeHttp:
    """Stands in for the Razorpay REST API; records calls, never touches the network."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json_body")))
        if url.endswith("/subscriptions") and method == "POST":
            return {"id": SUB_ID, "short_url": "https://rzp.io/i/e2e", "status": "created"}
        return {}


def _mail_text(queue_name: str) -> str:
    """Decoded text of every MIME part of an Email Queue row (bodies may be quoted-printable/base64)."""
    msg = message_from_string(frappe.db.get_value("Email Queue", queue_name, "message") or "")
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload:
            parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    return "\n".join(parts)


class BenchE2ERazorpay(FrappeTestCase):
    def setUp(self):
        from saas_core.api.v1 import auth
        from saas_core.gateways import registry
        from saas_core.provisioning import runner
        from saas_core.provisioning.fake_fcctl import FakeFcctl
        frappe.set_user("Administrator")
        self._purge()

        # conf: open signup (legacy flag removed so the signup_mode gate is really exercised)
        self._conf_backup = {k: frappe.conf.get(k) for k in
                             ("saas_core_site_domain", "saas_core_fake_fcctl", "saas_core_signup_mode",
                              "saas_core_public_signup", "saas_core_experimental")}
        frappe.conf["saas_core_site_domain"] = "apps.test"
        frappe.conf["saas_core_fake_fcctl"] = 1
        frappe.conf["saas_core_signup_mode"] = "open"
        frappe.conf["saas_core_experimental"] = "razorpay_subscription"  # this test exercises subscription mode
        frappe.conf.pop("saas_core_public_signup", None)

        # rate-limit counters live in redis and survive between runs
        from saas_core.tests._rl import clear_rate_limits
        clear_rate_limits()

        self.fake = FakeFcctl()
        runner.set_port(self.fake)

        # collect frappe.enqueue calls; they are drained synchronously by _drain()
        self.enqueued = []
        self._enqueue = frappe.enqueue
        frappe.enqueue = lambda *a, **k: self.enqueued.append((a, k))

        # signup should not need a real login session
        self._login_manager = auth._login_manager

        class _LM:
            def authenticate(self, **kw):
                pass

            def post_login(self):
                pass
        auth._login_manager = lambda: _LM()

        # Gateway Account: only ours may be enabled for razorpay (registry picks the first enabled)
        self._disabled = []
        for name in frappe.get_all("Gateway Account", filters={"gateway": "razorpay", "enabled": 1}, pluck="name"):
            frappe.db.set_value("Gateway Account", name, "enabled", 0)
            self._disabled.append(name)
        frappe.get_doc({"doctype": "Gateway Account", "account_name": ACCOUNT, "gateway": "razorpay",
                        "mode": "test", "enabled": 1, "checkout_mode": "subscription", "key_id": KEY_ID, "key_secret": KEY_SECRET,
                        "webhook_secret": WEBHOOK_SECRET}).insert(ignore_permissions=True)

        # P0-2: a customer's client `gateway` only counts when it is the effective default
        self._old_default_gw = frappe.conf.get("saas_core_default_gateway")
        frappe.conf["saas_core_default_gateway"] = "razorpay"

        # inject the fake http into the razorpay gateway built by the registry
        self.http = FakeHttp()
        self._rzp_cls = registry.GATEWAYS["razorpay"]
        http = self.http
        base = self._rzp_cls

        class _Rzp(base):
            def __init__(self, **kw):
                kw["http"] = http
                super().__init__(**kw)
        registry.GATEWAYS["razorpay"] = _Rzp

        self._seed()

    def tearDown(self):
        from saas_core.api.v1 import auth
        from saas_core.gateways import registry
        from saas_core.provisioning import runner
        frappe.enqueue = self._enqueue
        auth._login_manager = self._login_manager
        registry.GATEWAYS["razorpay"] = self._rzp_cls
        runner.set_port(None)
        frappe.set_user("Administrator")
        for k, v in self._conf_backup.items():
            if v is None:
                frappe.conf.pop(k, None)
            else:
                frappe.conf[k] = v
        if self._old_default_gw is None:
            frappe.conf.pop("saas_core_default_gateway", None)
        else:
            frappe.conf["saas_core_default_gateway"] = self._old_default_gw
        self._purge()
        for name in self._disabled:
            frappe.db.set_value("Gateway Account", name, "enabled", 1)
        frappe.db.commit()

    # ---------------------------------------------------------------- fixtures / cleanup
    def _seed(self):
        if not frappe.db.exists("Template", TPL):
            frappe.get_doc({"doctype": "Template", "slug": TPL, "title": "Acme",
                            "apps": [{"app": "frappe", "install_order": 1}],
                            "hooks": json.dumps([]), "site_config": json.dumps({})}).insert(ignore_permissions=True)
        if not frappe.db.exists("Plan", PLAN):
            frappe.get_doc({"doctype": "Plan", "code": PLAN, "title": PLAN, "template": TPL,
                            "fc_server_plan": f"fc-{PLAN}", "is_public": 1, "available": 1, "sort_order": 100,
                            "limits": json.dumps({"disk_gb": 50}),
                            "prices": [{"interval": "month", "currency": "INR", "amount": 99900,
                                        "gateway": "razorpay", "gateway_price_id": "plan_E2E"}]}
                           ).insert(ignore_permissions=True)

    def _purge(self):
        names = frappe.get_all("Tenant", filters={"name": ["like", f"{P}-%"]}, pluck="name")
        for dt in ("Provision Job", "Subscription", "Invoice", "API Token", "Tenant Member", "Support Ticket"):
            frappe.db.delete(dt, {"tenant": ["like", f"{P}-%"]})
        for dt in ("Payment Event",):
            frappe.db.delete(dt, {"tenant_ref": ["like", f"{P}-%"]})
        frappe.db.delete("Checkout Session", {"tenant": ["like", f"{P}-%"]})
        frappe.db.delete("Payment Event", {"event_key": ["in", [f"razorpay:{EVT_ID}", f"razorpay:{PAY_ID}"]]})
        for n in names:
            frappe.db.delete("Email Queue", {"reference_doctype": "Tenant", "reference_name": n})
        frappe.db.delete("Tenant", {"name": ["like", f"{P}-%"]})
        if frappe.db.exists("Gateway Account", ACCOUNT):
            frappe.delete_doc("Gateway Account", ACCOUNT, force=True, ignore_permissions=True)
        for dt, name in (("Plan", PLAN), ("Template", TPL)):
            if frappe.db.exists(dt, name):
                frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
        if frappe.db.exists("User", EMAIL):
            frappe.delete_doc("User", EMAIL, force=True, ignore_permissions=True)
        frappe.db.commit()

    # ---------------------------------------------------------------- helpers
    def _drain(self, only=("saas_core.billing.processor.process_payment_event",
                           "saas_core.notifications.welcome.send_tenant_setup")):
        """Run captured enqueues synchronously (only the named methods; provisioning is run by the test)."""
        pending, self.enqueued = self.enqueued, []
        ran = 0
        for a, k in pending:
            method = a[0] if a else k.get("method")
            if method in only:
                kwargs = {x: y for x, y in k.items() if x not in _ENQ_OPTS and x != "method"}
                frappe.get_attr(method)(**kwargs)
                ran += 1
        return ran

    def _webhook(self):
        body = json.dumps({
            "id": EVT_ID, "event": "subscription.charged",
            "payload": {
                "subscription": {"entity": {"id": SUB_ID, "customer_id": "cust_E2E", "notes": {"tenant": SLUG},
                                            "current_end": 1893456000}},
                "payment": {"entity": {"id": PAY_ID, "amount": 99900, "currency": "INR",
                                       "invoice_id": "inv_E2E0001", "subscription_id": SUB_ID}}}}).encode()
        headers = {"X-Razorpay-Signature": _hmac(WEBHOOK_SECRET, body), "X-Razorpay-Event-Id": EVT_ID}
        return headers, body

    def _jobs(self):
        return frappe.get_all("Provision Job", filters={"tenant": SLUG, "kind": "provision"}, pluck="name")

    def _welcome_rows(self):
        return frappe.get_all("Email Queue", filters={"reference_doctype": "Tenant", "reference_name": SLUG},
                              pluck="name")

    # ---------------------------------------------------------------- the test
    def test_signup_checkout_callback_webhook_provision_welcome(self):
        from saas_core import webhooks
        from saas_core.api.v1 import auth, checkout
        from saas_core.provisioning import runner

        # 1. signup (open mode, guest)
        frappe.set_user("Guest")
        me = auth.signup(email=EMAIL, password=PASSWORD, full_name="E2E Owner")
        self.assertNotIn("error", me)
        self.assertTrue(frappe.db.exists("User", EMAIL))

        # 2. create checkout session on razorpay
        frappe.set_user(EMAIL)
        co = checkout.create_session(plan=PLAN, interval="month", slug=SLUG, gateway="razorpay", **URLS)
        self.assertEqual(co["tenant"], SLUG)
        cp = co["client_payload"]
        self.assertEqual(cp["provider"], "razorpay")
        self.assertEqual(cp["key"], KEY_ID)
        self.assertEqual(cp["subscription_id"], SUB_ID)
        self.assertEqual(frappe.db.get_value("Tenant", SLUG, "status"), "pending")
        sent = [c for c in self.http.calls if c[1].endswith("/subscriptions")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2]["plan_id"], "plan_E2E")

        # 3. browser callback: signature = HMAC(key_secret, "<payment_id>|<subscription_id>")
        cb = {"razorpay_payment_id": PAY_ID, "razorpay_subscription_id": SUB_ID,
              "razorpay_signature": _hmac(KEY_SECRET, f"{PAY_ID}|{SUB_ID}".encode()),
              # hostile unsigned notes: the tenant/plan come from the server-side Checkout Session, not these
              "notes": {"tenant": "someone-else", "plan": "enterprise", "billing_mode": "invoice"}}
        bad = dict(cb, razorpay_signature="0" * 64)
        self.assertEqual(checkout.verify_payment(gateway="razorpay", payload=bad)["error"]["code"],
                         "invalid_signature")
        self.assertEqual(checkout.verify_payment(gateway="razorpay", payload=cb)["status"], "accepted")
        # a replay before the first event is processed is handled idempotently ("accepted"), after it "duplicate";
        # either way no second subscription or job may appear (asserted below)
        self.assertIn(checkout.verify_payment(gateway="razorpay", payload=cb)["status"], ("duplicate", "accepted"))
        self.assertEqual(frappe.db.get_value("Payment Event", f"razorpay:{PAY_ID}", "tenant_ref"), SLUG)
        self.assertTrue(frappe.db.exists("Checkout Session", f"razorpay:{SUB_ID}"))

        # 4. process the callback event synchronously: subscription active; the verified callback (paid=True)
        #    is proof of the first charge, so it provisions immediately (exactly one job)
        frappe.set_user("Administrator")
        self.assertEqual(self._drain(), 1)
        self.assertEqual(frappe.db.get_value("Payment Event", f"razorpay:{PAY_ID}", "status"), "processed")
        self.assertEqual(frappe.db.get_value("Subscription", {"tenant": SLUG}, "status"), "active")
        cb_jobs = self._jobs()
        self.assertEqual(len(cb_jobs), 1, f"expected exactly one provision job after callback, got {cb_jobs}")

        # 5. signed webhook (subscription.charged) -> payment.succeeded -> no second job (duplicate-safe)
        headers, body = self._webhook()
        self.assertEqual(webhooks.ingest("razorpay", headers, body)["status"], "accepted")
        self.assertEqual(self._drain(), 1)
        self.assertEqual(frappe.db.get_value("Payment Event", f"razorpay:{EVT_ID}", "status"), "processed")
        jobs = self._jobs()
        self.assertEqual(jobs, cb_jobs, f"expected the same single provision job after webhook, got {jobs}")

        # 6. run the pending Provision Job with the fake fcctl port
        self.assertEqual(frappe.db.get_value("Provision Job", jobs[0], "state"), "pending")
        self.assertEqual(runner.run_job(jobs[0], port=self.fake), "succeeded")
        self.assertEqual(frappe.db.get_value("Tenant", SLUG, "status"), "active")
        site = frappe.db.get_value("Tenant", SLUG, "fc_site")
        self.assertTrue(site)
        self.assertEqual(self.fake.count("provision_site"), 1)

        # 7. "Complete your setup" mail (welcome comes later, from tenants.setup_complete): queued once, setup link present, password absent
        self._drain()
        rows = self._welcome_rows()
        self.assertEqual(len(rows), 1, f"expected one welcome Email Queue row, got {rows}")
        text = _mail_text(rows[0])
        self.assertIn(f"https://{site}/update-password?key=", text)
        admin_pwd = self.fake._admin_passwords[site]
        self.assertNotIn(admin_pwd, text)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(KEY_SECRET, text)
        self.assertNotIn(WEBHOOK_SECRET, text)

        # 8. replaying the same signed webhook: duplicate, nothing new anywhere
        before_events = frappe.db.count("Payment Event", {"event_key": f"razorpay:{EVT_ID}"})
        self.assertEqual(webhooks.ingest("razorpay", headers, body)["status"], "duplicate")
        self.assertEqual(self._drain(), 0)
        self.assertEqual(self.enqueued, [])
        self.assertEqual(frappe.db.count("Payment Event", {"event_key": f"razorpay:{EVT_ID}"}), before_events)
        self.assertEqual(self._jobs(), jobs)
        self.assertEqual(self.fake.count("provision_site"), 1)
        self.assertEqual(self._welcome_rows(), rows)

    def test_forged_webhook_signature_rejected(self):
        from saas_core import webhooks
        headers, body = self._webhook()
        headers["X-Razorpay-Signature"] = _hmac("wrong-secret", body)
        with self.assertRaises(frappe.AuthenticationError):
            webhooks.ingest("razorpay", headers, body)
        self.assertFalse(frappe.db.exists("Payment Event", f"razorpay:{EVT_ID}"))
