# Payment gateways: overview

saas_core talks to payment providers only through the `Gateway` abstract class in
`saas_core/gateways/base.py`. Billing code never imports a provider module. Any installed Frappe app can add a
gateway without editing saas_core.

## Status of the gateways

| Name | Status | Capabilities | Notes |
| --- | --- | --- | --- |
| `razorpay` | Verified live (test mode); the only gateway verified | subscription, order, client_callback, refund, refund_lookup, payment_lookup, plan_update, invoice_lookup, payment_link | `checkout_mode=order` is the verified path. Subscription mode is experimental (`razorpay_subscription`). |
| `stripe` | Experimental: needs `saas_core_experimental=stripe`. Built from docs, never run live | subscription, portal | No refunds, so cancel-with-refund fails with a clear error. |
| `manual` | Mock payments, test mode only | subscription, client_callback, refund, plan_update | Never usable in live mode or next to a live account. |
| `fake` | Test/dev reference implementation. Needs `saas_core_experimental=fake_gateway` (not enabled by `all`) | all capabilities | In-memory, no network. Never allowed in live mode or next to a live account, same as `manual`. Source: `saas_core/gateways/fake.py`. |
| your plugin | Whatever you declare | whatever you declare | See [ADDING_A_GATEWAY.md](ADDING_A_GATEWAY.md). Policy: saas_core does not ship new real gateways; add yours in your own app. |

## Request flow

1. An admin creates a **Gateway Account** (DocType) with `gateway` = the gateway name, `mode` = `test` or `live`,
   `key_id`, `key_secret`, `webhook_secret` (passwords are stored encrypted), optional `extra` JSON.
2. `saas_core/gateways/policy.py::resolve_gateway` picks the gateway name. The client's choice is ignored unless
   it equals the default or the caller is System Manager. Default: site_config `saas_core_default_gateway`, else the
   first enabled Gateway Account, else `razorpay`.
3. `saas_core/gateways/registry.py::get_gateway(name)` finds the class (built-ins in `GATEWAYS`, plus every
   `saas_core_gateways` hook entry), checks the experimental gate, loads the enabled Gateway Account and builds
   `cls(key_id, key_secret, webhook_secret, extra)`.
4. Checkout: `api/v1/checkout.py` calls `gw.create_checkout_session(CheckoutParams)`, stores a Checkout Session
   keyed by `CheckoutResult.session_ref`, returns `url` and `gw.client_payload(result)` to the browser.
5. Payment completes. Two proofs can arrive, both ending in one `Payment Event` row keyed `gateway:event_id`:
   - Browser callback: `api/v1/checkout_verify.py` calls `gw.verify_client_callback(payload)` (capability `client_callback`).
   - Webhook: the provider POSTs to `/api/method/saas_core.webhooks.<razorpay|stripe>` or, for any gateway,
     `/api/method/saas_core.webhooks.receive?gateway=<name>`. `webhooks.ingest` calls `gw.verify_webhook(headers, raw_body)`,
     then `gw.parse_event(payload)`, stores the raw body once and enqueues `billing/processor.py`.
6. `billing/processor.py` turns the `NormalizedEvent` into Subscription/Invoice changes (activate, renew, dunning, cancel).
7. Later operations call optional methods, each guarded by `supports`: refunds (`billing/refunds.py`,
   `cancellation.py`), plan change (`plan_change.py`), invoice-mode renewal (`processor.py`).
   An unsupported capability yields a clear `GatewayCapabilityError`, never an `AttributeError`.

## Files

| Path | Role |
| --- | --- |
| `saas_core/gateways/base.py` | `Gateway` ABC, dataclasses, `KINDS`, `CAPABILITIES`, `require_capability`, HMAC helpers |
| `saas_core/gateways/registry.py` | Class lookup (built-in + hook), validation, experimental gate, account loading |
| `saas_core/gateways/razorpay.py`, `stripe.py`, `manual.py` | Built-in gateways |
| `saas_core/gateways/fake.py` | In-memory reference implementation |
| `saas_core/gateways/conformance.py` | Reusable contract tests for plugin authors |
| `saas_core/webhooks.py` | Webhook receivers (`receive` is the generic one) |
| `tests/test_gateway_conformance_kit.py` | Kit run against razorpay (HTTP mocked), stripe and fake |
| `tests/test_gateway_plugins.py` | Hook registration, validation, capability checks |

## Next

- Contract details: [CONTRACT.md](CONTRACT.md)
- Build one: [ADDING_A_GATEWAY.md](ADDING_A_GATEWAY.md)
- Hand it to a coding agent: [AGENT_PROMPT.md](AGENT_PROMPT.md)
