# Adding a gateway from your own app

Goal: a new payment provider `mygw` works with saas_core checkout, webhooks, refunds and renewals, with **no edit
to saas_core**. Everything lives in your own Frappe app (`my_app` below). Read [CONTRACT.md](CONTRACT.md) first.

Policy: saas_core does not accept new real gateways in its own tree. Plugins are the supported path. Only
Razorpay is verified live; treat anything you add as untested until you have run it in the provider's test mode.

Estimated files: 3 (gateway module, hooks line, test file).

## Step 1. Create the gateway module

Path: `my_app/my_app/gateways/mygw.py` (any importable path works; it must match the hook in step 2).

The skeleton below is real, runnable code; `tests/test_docs_skeleton.py` in saas_core executes it and runs the
conformance kit against it, so it stays correct. Replace the endpoints, header names and field names with your
provider's. Keep the structure.

<!-- skeleton -->
```python
"""MyGw payment gateway for saas_core. Test keys only until verified."""
from saas_core.gateways.base import (CheckoutParams, CheckoutResult, Gateway, GatewayError, NormalizedEvent,
                                     hmac_hex, lower_headers, safe_equal)

API = "https://api.mygw.example/v1"
SIGNATURE_HEADER = "x-mygw-signature"
EVENT_KINDS = {                      # provider event type -> NormalizedEvent kind
    "payment.paid": "payment.succeeded",
    "payment.failed": "payment.failed",
    "checkout.done": "checkout.completed",
    "refund.done": "refund",
}


class MyGwGateway(Gateway):
    name = "mygw"                                     # == hook key == Gateway Account.gateway
    supports = frozenset({"order", "refund", "refund_lookup", "payment_lookup"})   # only what you implement

    def _headers(self, **extra):
        return {"Authorization": f"Bearer {self._key_secret}", **extra}

    # ---- checkout ----------------------------------------------------------------------------------
    def create_checkout_session(self, params: CheckoutParams) -> CheckoutResult:
        meta = params.metadata or {}
        currency = str(meta.get("currency") or "").upper()
        amount = meta.get("amount_minor")
        if not currency:
            raise GatewayError("checkout currency missing")           # never default a currency
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise GatewayError("checkout amount must be a positive integer (minor units)")
        res = self.http.request("POST", f"{API}/checkouts", headers=self._headers(), json_body={
            "amount": amount, "currency": currency, "reference": f"{params.tenant}-{params.plan}",
            "success_url": params.success_url, "cancel_url": params.cancel_url,
            "customer_email": params.customer_email, "metadata": {"tenant": params.tenant, "plan": params.plan}})
        if not res.get("id"):
            raise GatewayError("mygw checkout creation failed")
        return CheckoutResult(url=res.get("url", ""), session_ref=res["id"], raw=res)

    def client_payload(self, session) -> dict:
        return {"provider": self.name, "key": self._key_id}          # public values only

    def portal_link(self, customer_ref: str, return_url: str) -> str:
        return return_url                                            # no hosted portal

    def cancel_subscription(self, sub_ref: str, at_period_end: bool) -> None:
        return None                                                  # order mode only: nothing to cancel

    # ---- webhooks ----------------------------------------------------------------------------------
    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        sig = lower_headers(headers).get(SIGNATURE_HEADER)
        if not sig or not self._webhook_secret:
            return False
        return safe_equal(hmac_hex(self._webhook_secret, raw_body), str(sig).strip())

    def parse_event(self, payload: dict, headers: dict | None = None) -> NormalizedEvent:
        obj = payload.get("data") or {}
        try:
            amount = int(obj.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        return NormalizedEvent(
            event_id=str(payload.get("id") or ""),                    # from the signed body only
            gateway=self.name,
            kind=EVENT_KINDS.get(str(payload.get("type") or ""), "ignored"),
            invoice_ref=obj.get("checkout_id"),                       # must equal CheckoutResult.session_ref
            tenant_ref=(obj.get("metadata") or {}).get("tenant"),
            amount=amount, currency=obj.get("currency"), raw=payload)

    # ---- refunds -----------------------------------------------------------------------------------
    def refund_payment(self, payment_id: str, amount_minor: int, *, notes: dict | None = None,
                       receipt: str = "") -> dict:
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
            raise GatewayError("refund amount must be a positive integer (minor units)")
        res = self.http.request(
            "POST", f"{API}/payments/{payment_id}/refunds",
            headers=self._headers(**{"Idempotency-Key": receipt} if receipt else {}),
            json_body={"amount": amount_minor, "reference": receipt, "metadata": dict(notes or {})}) or {}
        return {"id": res.get("id"), "status": res.get("status") or "pending",
                "amount": int(res.get("amount") or amount_minor), "payment_id": payment_id}

    def list_payment_refunds(self, payment_id: str) -> list[dict]:
        res = self.http.request("GET", f"{API}/payments/{payment_id}/refunds", headers=self._headers()) or {}
        return [{"id": r.get("id"), "status": r.get("status"), "amount": int(r.get("amount") or 0),
                 "payment_id": payment_id, "receipt": r.get("reference"), "notes": r.get("metadata") or {}}
                for r in res.get("items") or []]

    def get_payment(self, payment_id: str) -> dict:
        res = self.http.request("GET", f"{API}/payments/{payment_id}", headers=self._headers()) or {}
        return {"amount": int(res.get("amount") or 0), "amount_refunded": int(res.get("amount_refunded") or 0),
                "currency": (str(res["currency"]).upper() if res.get("currency") else None)}
```

Rules the skeleton follows (keep them):

- `name` equals the hook key.
- `supports` lists only implemented capabilities. Add `subscription`, `client_callback`, `plan_update`,
  `invoice_lookup`, `payment_link`, `portal` only with the matching method (see the table in CONTRACT.md).
- All HTTP goes through `self.http.request`; secrets only in the `Authorization` header, never in messages.
- `event_id` comes from the signed body. `session_ref` must equal the id your events carry back (here
  `data.checkout_id`, stored in `invoice_ref`; if your events carry a subscription id use `subscription_ref`).

## Step 2. Register it in your app's `hooks.py`

Path: `my_app/my_app/hooks.py`. Add exactly one line (dotted path to the class):

```python
saas_core_gateways = {"mygw": "my_app.gateways.mygw.MyGwGateway"}
```

Several gateways: add more keys to the same dict. A plugin cannot reuse the names `razorpay`, `stripe`, `manual`,
`fake`. After editing hooks run `bench --site <site> migrate` (or `bench clear-cache` + restart) so Frappe reloads hooks.

saas_core validates the class when the gateway is first used: subclass of `Gateway`, `name == key`, `supports` is a
`frozenset` of known capabilities, all abstract methods implemented. Failures raise a clear `ValidationError`.

## Step 3. Create the Gateway Account

In Desk, open **Gateway Account** and create one record:

| Field | Value |
| --- | --- |
| Account Name | `mygw-test` |
| Gateway | `mygw` (exactly the hook key) |
| Mode | `test` |
| Key Id | the provider's public/publishable key (test) |
| Key Secret | the provider's secret key (test). Stored encrypted |
| Webhook Secret | the signing secret the provider gives for your webhook endpoint |
| Checkout Mode | `order` |
| Enabled | checked |

Make it the default for checkout: `bench --site <site> set-config saas_core_default_gateway mygw`
(otherwise the first enabled Gateway Account wins). Plan Price rows for the plan should carry the currency you
test with; set `gateway` on a row only if prices differ per gateway.

Only one live gateway should be enabled at a time; the mock `manual` gateway is disabled next to a live account.

## Step 4. Point the provider's webhook at saas_core

Webhook URL (POST, no auth, signature checked by your `verify_webhook`):

```
https://<your-site>/api/method/saas_core.webhooks.receive?gateway=mygw
```

Subscribe the webhook to the provider events you mapped in `EVENT_KINDS`. For local testing expose the site with a
tunnel and use the provider's test mode. `scripts/tunnel_webhook_check.sh` in saas_core shows the pattern.

## Step 5. Write and run the conformance test

Path: `my_app/my_app/tests/test_mygw_conformance.py`. The base class is `saas_core.gateways.conformance.GatewayConformance`;
the class name must start with `Test`.

<!-- conformance-test -->
```python
import json

from my_app.gateways.mygw import MyGwGateway
from saas_core.gateways.base import hmac_hex
from saas_core.gateways.conformance import GatewayConformance

WH, KS = "whsec_test", "sk_test_x"


class FakeHttp:
    """In-memory stand-in for the provider API. Mimics the real API, including that it does not dedupe on its own."""

    def __init__(self):
        self.payments = {"pay_1": {"amount": 4900, "amount_refunded": 0, "currency": "USD"}}
        self.refunds = []

    def request(self, method, url, **kw):
        path = url.split("/v1/", 1)[1]
        body = kw.get("json_body") or {}
        if method == "POST" and path == "checkouts":
            return {"id": "co_1", "url": "https://pay.mygw.example/co_1"}
        if method == "POST" and path.endswith("/refunds"):
            pid = path.split("/")[1]
            r = {"id": f"re_{len(self.refunds) + 1}", "status": "processed", "amount": body["amount"],
                 "reference": body.get("reference"), "metadata": body.get("metadata"), "payment_id": pid}
            self.refunds.append(r)
            self.payments[pid]["amount_refunded"] += body["amount"]
            return r
        if method == "GET" and path.endswith("/refunds"):
            return {"items": self.refunds}
        if method == "GET" and path.startswith("payments/"):
            return dict(self.payments[path.split("/")[1]])
        raise AssertionError(f"unmocked call {method} {url}")


class TestMyGwConformance(GatewayConformance):
    webhook_secret, key_secret = WH, KS

    def make_gateway(self, **overrides):
        kw = dict(key_id="pk_test_x", key_secret=KS, webhook_secret=WH, http=FakeHttp())
        kw.update(overrides)
        return MyGwGateway(**kw)

    def sign_webhook(self, gw, raw_body):
        return {"X-MyGw-Signature": hmac_hex(WH, raw_body)}

    def event_samples(self):
        data = {"checkout_id": "co_1", "amount": 4900, "currency": "USD", "metadata": {"tenant": "acme"}}
        return {
            "payment.succeeded": {"id": "evt_1", "type": "payment.paid", "data": data},
            "payment.failed": {"id": "evt_2", "type": "payment.failed", "data": data},
            "checkout.completed": {"id": "evt_3", "type": "checkout.done", "data": data},
            "refund": {"id": "evt_4", "type": "refund.done", "data": {"amount": 100, "currency": "USD"}},
            "ignored": {"id": "evt_5", "type": "customer.updated", "data": {}},
        }

    def refundable_payment(self, gw):
        return "pay_1", 4900
```

Run (from the bench root, with saas_core installed in the same environment):

```bash
cd apps/my_app && python -m pytest -q my_app/tests/test_mygw_conformance.py
```

The kit does not need a Frappe site or network: it runs with `http` faked. It checks signature accept/reject
(tampered body, missing, bad, wrong secret, empty secret), `parse_event` for every kind you list in
`event_samples()`, unknown events become `ignored`, checkout result shape and `client_payload` free of secrets,
currency required, refund amount validation, refund result shape, same-receipt-no-double-refund, payment lookup shape,
and that `supports` matches the methods you override. Hooks you may override: `checkout_params`,
`unknown_event_payload`, `valid_client_callback` (required if you declare `client_callback`),
`refundable_payment` (required if you declare `refund` or `payment_lookup`).

Pass criteria: zero failures; skipped tests must each be explained by a capability you did not declare.

## Step 6. Test-mode checklist (before any real traffic)

Use the provider's **test** keys only. Do not move to live until every box is ticked and the owner approves.

- [ ] Gateway Account has `Mode = test`; keys are the provider's test keys; no live key anywhere in the repo or site_config.
- [ ] Conformance kit passes.
- [ ] Create a checkout from the app (or `checkout.create_session`) and open the returned `url` or complete the client payload with a provider test card.
- [ ] The webhook arrives; a `Payment Event` named `mygw:<event_id>` appears with status `processed` (Desk list view).
- [ ] Replaying the same webhook (provider dashboard "resend") returns `{"status": "duplicate"}` and changes nothing.
- [ ] A webhook with a tampered body or wrong signature gets `401` and creates no Payment Event.
- [ ] The Subscription becomes `active`, an Invoice is `paid`, amount and currency equal the Plan Price row.
- [ ] Cancel with refund: a Refund doc reaches `processed`; running the cancel sweep again does not create a second refund at the provider.
- [ ] A failed-payment test card produces `payment.failed` (dunning path) if you declared it.
- [ ] Logs and the Error Log contain no key, secret or signature (search for the key prefix).
- [ ] Capabilities you did not declare produce the clear "does not support" message, not a stack trace.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Unknown gateway: mygw` | Hook missing or not reloaded; run `bench --site <site> migrate` and restart. |
| `Gateway plugin 'mygw': ... must equal the hook key` | `name` on the class differs from the hook key. |
| `unknown capabilities [...]` | A `supports` entry is not in `CAPABILITIES`. |
| `No enabled Gateway Account for mygw` | Create/enable the account in step 3. |
| Webhook returns `400 Invalid webhook request` | Gateway name wrong in the URL, or no enabled account. |
| Webhook returns `401` | Wrong webhook secret, body changed by a proxy, or header name differs from `SIGNATURE_HEADER`. |
