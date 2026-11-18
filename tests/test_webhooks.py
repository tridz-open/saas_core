import hashlib, hmac, json, types, pytest

SEC = "whsec_topsecret"


def setup_gw(frappe, gateway="razorpay"):
    doc = frappe._Doc(doctype="Gateway Account", name="a1", gateway=gateway, enabled=1, key_id="kid")
    doc.get_password = lambda f, raise_exception=True: {"key_secret": "KEYSECRET", "webhook_secret": SEC}[f]
    doc.insert()


def rz_req(event="subscription.charged", eid="evt_1", secret=SEC):
    body = json.dumps({"event": event, "payload": {"subscription": {"entity": {"id": "sub_1"}}}}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": eid}, body


def test_accept_then_replay(frappe):
    setup_gw(frappe)
    import importlib; webhooks = importlib.import_module("saas_core.webhooks")
    h, b = rz_req()
    assert webhooks.ingest("razorpay", h, b) == {"status": "accepted"}
    assert webhooks.ingest("razorpay", h, b) == {"status": "duplicate"}
    docs = [d for (dt, _), d in frappe._store.docs.items() if dt == "Payment Event"]
    assert len(docs) == 1 and docs[0].name == "razorpay:subscription.charged:sub_1"  # signed-body id, not the header and docs[0].status == "received"
    assert docs[0].raw_payload == b.decode() and docs[0].kind == "payment.succeeded"
    assert frappe._store.enqueued == [("saas_core.billing.processor.process_payment_event", {"payment_event": "razorpay:subscription.charged:sub_1", "enqueue_after_commit": True})]


def test_bad_and_tampered_signature(frappe):
    setup_gw(frappe)
    import importlib; webhooks = importlib.import_module("saas_core.webhooks")
    h, b = rz_req(secret="wrong")
    with pytest.raises(frappe.AuthenticationError) as ei:
        webhooks.ingest("razorpay", h, b)
    assert frappe.local.response["http_status_code"] == 401
    assert SEC not in str(ei.value) and "KEYSECRET" not in str(ei.value)
    h, b = rz_req()
    with pytest.raises(frappe.AuthenticationError):
        webhooks.ingest("razorpay", h, b + b" ")
    with pytest.raises(frappe.AuthenticationError):
        webhooks.ingest("razorpay", {}, b)
    assert not frappe._store.docs.keys() - {("Gateway Account", "a1")}
    assert frappe._store.enqueued == []


def test_unknown_kind_ignored(frappe):
    setup_gw(frappe)
    import importlib; webhooks = importlib.import_module("saas_core.webhooks")
    h, b = rz_req(event="refund.created", eid="evt_2")
    assert webhooks.ingest("razorpay", h, b) == {"status": "ignored"}
    evs = [d for (dt, _), d in frappe._store.docs.items() if dt == "Payment Event"]
    assert len(evs) == 1 and evs[0].status == "ignored"
    assert frappe._store.enqueued == []


def test_stripe_tolerance_via_ingest(frappe, monkeypatch):
    setup_gw(frappe, "stripe")
    import importlib; webhooks = importlib.import_module("saas_core.webhooks")
    import importlib; registry = importlib.import_module("saas_core.gateways.registry")
    from saas_core.gateways.stripe import StripeGateway
    monkeypatch.setattr(registry, "get_gateway", lambda n, a=None: StripeGateway(webhook_secret=SEC, clock=lambda: 2000))
    body = json.dumps({"id": "evt_s", "type": "invoice.paid", "data": {"object": {"id": "in_1", "subscription": "s"}}}).encode()
    def hdr(t): return {"Stripe-Signature": f"t={t},v1=" + hmac.new(SEC.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()}
    with pytest.raises(frappe.AuthenticationError):
        webhooks.ingest("stripe", hdr(1000), body)
    assert webhooks.ingest("stripe", hdr(1900), body) == {"status": "accepted"}


def test_endpoint_reads_request(frappe):
    setup_gw(frappe)
    import importlib; webhooks = importlib.import_module("saas_core.webhooks")
    h, b = rz_req(eid="evt_3")
    frappe.request = types.SimpleNamespace(headers=h, get_data=lambda: b)
    assert webhooks.razorpay() == {"status": "accepted"}


def test_bad_payload(frappe):
    setup_gw(frappe)
    import importlib; webhooks = importlib.import_module("saas_core.webhooks")
    b = b"not json"
    sig = hmac.new(SEC.encode(), b, hashlib.sha256).hexdigest()
    with pytest.raises(frappe.ValidationError):
        webhooks.ingest("razorpay", {"X-Razorpay-Signature": sig}, b)
    assert frappe.local.response["http_status_code"] == 400
