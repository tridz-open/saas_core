def test_ignored_webhook_does_not_block_callback(frappe):
    from saas_core import webhooks
    from saas_core.gateways.base import NormalizedEvent
    ign = NormalizedEvent(event_id="pay_X", gateway="razorpay", kind="ignored", raw={"event": "payment.authorized"})
    doc1, new1 = webhooks.record_event("razorpay", ign, "{}")
    assert new1 and doc1.name != "razorpay:pay_X"
    cb = NormalizedEvent(event_id="pay_X", gateway="razorpay", kind="checkout.completed", raw={"paid": True})
    doc2, new2 = webhooks.record_event("razorpay", cb, "{}")
    assert new2 and doc2.name == "razorpay:pay_X"
