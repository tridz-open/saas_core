# Agent prompt: add a payment gateway

How to use: copy everything inside the fenced block below into your coding agent (Claude Code, Codex, Cursor, etc.),
run it from the root of **your own Frappe app repo** (the app that will hold the gateway), and replace the three
`<<...>>` placeholders. Do not run it inside the saas_core repo: saas_core does not accept new real gateways.

Placeholders:

- `<<GATEWAY_NAME>>`: lowercase id, `^[a-z][a-z0-9_]{0,31}$`, not `razorpay`, `stripe`, `manual`, `fake` (example: `mollie`).
- `<<PROVIDER_DOCS_URL>>`: the provider's official API documentation URL.
- `<<CAPABILITIES>>`: comma-separated subset of `subscription, order, portal, client_callback, refund, refund_lookup, payment_lookup, plan_update, invoice_lookup, payment_link` (example: `order, refund, refund_lookup, payment_lookup`). When unsure, use `order, refund, refund_lookup, payment_lookup`.

```text
TASK
Add a payment gateway named "<<GATEWAY_NAME>>" to this Frappe app as a plugin for saas_core. The provider's API docs
are at <<PROVIDER_DOCS_URL>>. Implement exactly these capabilities: <<CAPABILITIES>>. Do NOT edit saas_core itself.

READ FIRST (in this order, fully, before writing code)
1. saas_core docs/gateways/README.md, CONTRACT.md and ADDING_A_GATEWAY.md. Find them in the installed saas_core
   package checkout (for example apps/saas_core/docs/gateways/) or at
   https://github.com/tridz-open/saas_core/tree/main/docs/gateways
2. saas_core/gateways/base.py: the Gateway ABC, CAPABILITIES, KINDS, NormalizedEvent, helpers hmac_hex, safe_equal,
   lower_headers, require_capability.
3. saas_core/gateways/fake.py: a complete in-memory implementation of every method. Mirror its shape.
4. saas_core/gateways/razorpay.py: the only gateway verified live; follow its style for HTTP and errors.
5. saas_core/gateways/conformance.py: the test kit you must pass.
6. The provider docs: authentication, create payment/checkout, webhook signing and event list, refunds, test mode.

BUILD
1. Create <app>/<app>/gateways/<<GATEWAY_NAME>>.py containing class <ClassName>(Gateway) with
   name = "<<GATEWAY_NAME>>" and supports = frozenset({<<CAPABILITIES>>}) (as quoted strings).
   Implement the five abstract methods and exactly the optional methods your capabilities require. Do not declare a
   capability you do not implement; do not implement a method whose capability you do not declare.
2. Add one line to <app>/<app>/hooks.py:
   saas_core_gateways = {"<<GATEWAY_NAME>>": "<app>.gateways.<<GATEWAY_NAME>>.<ClassName>"}
   (merge into the existing dict if the line already exists).
3. Create <app>/<app>/tests/test_<<GATEWAY_NAME>>_conformance.py: a class TestXxx(GatewayConformance) from
   saas_core.gateways.conformance with make_gateway, sign_webhook, event_samples (every NormalizedEvent kind the
   provider can produce, plus "ignored"), and refundable_payment / valid_client_callback if you declared those
   capabilities. Use an in-memory FakeHttp, modelled on the one in ADDING_A_GATEWAY.md. No network in tests.
4. Add a short <app>/docs or README section: Gateway Account fields to fill, webhook URL
   https://<site>/api/method/saas_core.webhooks.receive?gateway=<<GATEWAY_NAME>>, events to subscribe to.

RULES THAT MUST HOLD (the kit and reviewers check these)
- Money: integer minor units only; currency required, from the plan price (metadata["currency"]); if missing raise
  GatewayError. Never default or convert a currency. Never accept bool/float amounts. Amounts come from
  CheckoutParams.metadata["amount_minor"], never from the browser.
- verify_webhook: HMAC (or the provider's scheme) over the exact raw bytes with safe_equal; return False, never
  raise, on missing/malformed/wrong signature and when webhook_secret is empty; enforce the provider's timestamp
  tolerance if it signs one.
- parse_event: event_id non-empty and derived only from the signed body; unknown events -> kind "ignored"; amount int;
  currency 3-letter code or None; raw holds the provider payload with no secrets.
- Refunds: pass `receipt` to the provider as its idempotency key/reference; list_payment_refunds must echo it in
  "receipt". Same receipt must never produce two refunds.
- Errors: raise GatewayError for provider failures. No secret, key, signature, token or full body in any message,
  log line or exception. Use frappe.log_error only with a short fixed string.
- All network calls through self.http.request(...). No requests/urllib calls elsewhere, none at import time.
- Use the provider's TEST mode and TEST keys only.

SAFETY (hard rules)
- Never use, ask for, print or commit live keys. Never make a real charge, refund, payout or transfer. Never call the
  provider's live endpoint.
- Never write a key, secret or webhook secret into a file, commit, test, log or chat message. Tests use dummy values.
- If you need real test credentials (for the manual test-mode checklist), STOP and ask me for them. Say exactly which
  values you need (test key id, test secret, webhook signing secret) and wait. Do not invent values and do not read
  secrets from environment files without being told to.
- If the provider docs are ambiguous about signing, idempotency or amount units, STOP and ask instead of guessing.
- Do not install system packages or modify other apps.

DONE WHEN (run and show the output of each)
1. python -m pytest -q <app>/tests/test_<<GATEWAY_NAME>>_conformance.py  -> 0 failures. Every skipped test must be
   explained by a capability you did not declare.
2. With the hook added, `bench --site <site> execute saas_core.gateways.registry.available_gateways` lists
   "<<GATEWAY_NAME>>" (skip if no bench is available and say so).
3. Your app's existing test suite still passes.
4. A final report with: files created/changed, capabilities declared vs implemented, any provider behaviour you were
   unsure about, and the unticked items of the test-mode checklist in ADDING_A_GATEWAY.md (step 6) that need me.
```

Notes for the human:

- The agent stops and asks for credentials; give test keys only, and enter them yourself in the Gateway Account form.
- Review `verify_webhook`, `refund_payment` and `create_checkout_session` by hand: they handle money.
- Go live only after the step 6 checklist in [ADDING_A_GATEWAY.md](ADDING_A_GATEWAY.md) is complete.
