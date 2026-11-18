# Gateway contract

Authoritative source: `saas_core/gateways/base.py` (docstrings). This page restates it. If the two disagree, the
code and the conformance kit (`saas_core/gateways/conformance.py`) win.

## Class requirements

- Subclass `saas_core.gateways.base.Gateway`.
- `name`: non-empty `str`, equal to the key you use in the `saas_core_gateways` hook and in the Gateway Account's `gateway` field. Lowercase letters, digits, underscore; starts with a letter; max 32 chars (the generic webhook route enforces `^[a-z][a-z0-9_]{0,31}$`).
- `supports`: a `frozenset` of keys of `CAPABILITIES`.
- Optional `experimental_feature = "<name from saas_core.experimental.FEATURES>"` to gate your gateway behind the experimental flag.
- Constructor is inherited: `Gateway(key_id, key_secret, webhook_secret, http=None, extra=None)`. Do not change its signature; the registry calls it with those keywords. Use `self.http.request(...)` for all network calls so tests can inject a fake.

## Capabilities (`base.CAPABILITIES`)

Declare a capability if and only if you implement it. The conformance test `test_capability_honesty` fails otherwise.
Billing checks the capability before calling the method. What happens when it is missing depends on the caller, see
the "If missing" column below (for example a refund is marked failed, a cancel skips the lookup, the browser callback
answers 422). Code that calls a missing method directly (or `require_capability`) gets `GatewayCapabilityError`
(a `GatewayError` and a `NotImplementedError`), with `.code == "capability_unsupported"`.

| Capability | Method it promises | Used by | If missing |
| --- | --- | --- | --- |
| `subscription` | `create_checkout_session` can start a recurring subscription | checkout | You can still do `order` |
| `order` | `create_checkout_session` can take a one-time fixed amount | checkout | Declare at least one of `subscription`/`order` |
| `portal` | `portal_link` returns a hosted page (not just `return_url`) | payment-methods API | `return_url` is used |
| `client_callback` | `verify_client_callback` | `api/v1/checkout_verify.py` | Endpoint answers 422; payments confirm via webhook only |
| `refund` | `refund_payment` | cancellation, plan-change refunds | Refund doc fails, non-retryable: "does not support refunds"; subscription flagged `refund_failed` |
| `refund_lookup` | `list_payment_refunds` | refund retry | A retry of a refund that may have reached the provider is refused (no blind second refund) |
| `payment_lookup` | `get_payment` | refund over-balance check | Check skipped; the provider must reject an over-refund itself |
| `plan_update` | `update_subscription_plan` | plan change | Local schedule stands; provider plan is not changed |
| `invoice_lookup` | `invoice_payment_id` | finding the payment to refund | Payment unknown; cancel flags `refund_payment_unknown` |
| `payment_link` | `create_payment_link` | invoice-mode renewals | Renewal invoice cannot be issued (clear error) |

## Methods

Abstract (must implement):

| Method | Input | Output | Rules |
| --- | --- | --- | --- |
| `create_checkout_session(params)` | `CheckoutParams` | `CheckoutResult(url, session_ref, raw)` | `session_ref` non-empty. Charge `params.metadata["amount_minor"]` in `params.metadata["currency"]`; missing currency raises `GatewayError`. |
| `portal_link(customer_ref, return_url)` | strs | URL str | Return `return_url` if no portal. |
| `cancel_subscription(sub_ref, at_period_end)` | str, bool | `None` | Idempotent. No-op if you have no provider subscriptions. |
| `verify_webhook(headers, raw_body)` | dict, bytes | `bool` | See [Webhook security](#webhook-security). Never raises. |
| `parse_event(payload, headers=None)` | dict | `NormalizedEvent` | See [Event kinds](#normalizedevent-kinds). Never raises on unknown events. |

Optional (override to enable; defaults raise `GatewayCapabilityError` unless noted):

| Method | Capability | Returns |
| --- | --- | --- |
| `client_payload(session)` | none (default `{"provider": name}`) | dict with `provider`; only public values |
| `verify_client_callback(payload)` | `client_callback` | `NormalizedEvent(kind="checkout.completed")`; raise `GatewayError` if the proof is invalid |
| `refund_payment(payment_id, amount_minor, *, notes=None, receipt="")` | `refund` | `{"id","status","amount","payment_id"}`; status in `processed`, `pending`, `failed` |
| `list_payment_refunds(payment_id)` | `refund_lookup` | `[{"id","status","amount","payment_id","receipt","notes"}]` |
| `ensure_captured(payment_id)` | none (default no-op returning captured) | dict; raise `GatewayError` if it cannot be captured |
| `get_payment(payment_id)` | `payment_lookup` | `{"amount","amount_refunded","currency"}` |
| `update_subscription_plan(sub_ref, plan_id, *, at_cycle_end=True)` | `plan_update` | provider dict |
| `invoice_payment_id(invoice_id)` | `invoice_lookup` | payment id str or `None` |
| `create_payment_link(*, amount, currency, description, email, notes=None, callback_url="", reference_id="")` | `payment_link` | `{"id","short_url",...}` |

## `NormalizedEvent` kinds

`base.KINDS`. `parse_event` must map each provider event to exactly one of these. Anything you do not map is `"ignored"`.

| Kind | Meaning | Fields billing relies on |
| --- | --- | --- |
| `checkout.completed` | Customer authorised or paid at checkout | `subscription_ref` or `invoice_ref` (must match a stored `session_ref`), `tenant_ref` if known |
| `payment.succeeded` | A charge or renewal succeeded | `subscription_ref`, `amount`, `currency`, `period_end`, `invoice_ref` |
| `payment.failed` | A charge failed (starts dunning) | `subscription_ref`, `amount`, `currency` |
| `subscription.canceled` | Provider subscription ended | `subscription_ref` |
| `subscription.updated` | Provider subscription changed | `subscription_ref`, `period_end` |
| `refund` | A refund processed or failed | `amount`, `currency`; the raw refund entity in `raw` |
| `ignored` | Not actionable. Stored for audit, never processed | none |

Field rules for every event: `event_id` non-empty `str` derived only from the signed body (the same webhook replayed
gives the same id; unsigned headers must not feed it); `gateway` equals `name`; `amount` is an `int >= 0` in minor
units (0 if unknown); `currency` is a 3-letter ISO 4217 code in any case or `None` (billing upper-cases); `raw` is the
provider payload, stored verbatim, so it must contain no secrets.

Note: the refund processor (`billing/processor.py::_refund`) currently reads a Razorpay-shaped `raw`
(`payload.refund.entity` with `id`, `payment_id`, `amount`, `status`, `receipt`, `notes.refund_doc`). A plugin that
wants provider-initiated refund webhooks to book against a Refund doc should put an equivalent structure in `raw`,
or leave refund events as `ignored`; the refunds your code initiates are booked from the `refund_payment` result.

## Money rules

1. Integer minor units everywhere (cents, paise). Never `float`, never `bool`. `refund_payment` rejects `0`, negatives, bools, floats and strings with `GatewayError`.
2. Every amount travels with a currency. The charge currency is the currency of the Plan Price row chosen for the customer (`params.metadata["currency"]`). If it is missing, raise `GatewayError`. Never default to a currency, and never fall back to another currency's price (saas_core raises `PriceNotFound`).
3. A refund is in the payment's currency. Billing refuses a refund whose currency differs from the payment (when `payment_lookup` is declared).
4. Amounts come from server-side Plan Price rows, never from the browser. Never trust `amount`, `tenant` or `plan` in a client callback payload.
5. Zero-decimal and three-decimal currencies: convert to the provider's expected unit inside your adapter; the rest of saas_core only sees minor units as stored in the Plan Price.

## Webhook security

1. Verify the signature over the exact raw bytes (`frappe.request.get_data()`), before any JSON parsing. Use `hmac_hex` and `safe_equal` from `base.py` (constant-time compare).
2. Return `False` (do not raise) for: no signature header, malformed signature, empty `webhook_secret`, wrong secret, modified body. The webhook layer answers `401` for `False`.
3. If the provider signs a timestamp, reject stale ones (Stripe: 300 s tolerance; injectable clock for tests).
4. Header names are case-insensitive: use `lower_headers`.
5. Idempotency of delivery: `webhooks.record_event` stores `Payment Event` keyed `<gateway>:<event_id>` and only the first insert enqueues processing. Duplicates return `{"status": "duplicate"}`.
6. Unknown gateway names and gateways without an enabled account get a generic `400 Invalid webhook request` (no configuration leak). Guest endpoint is rate-limited per IP.
7. Webhook URL for plugin gateways: `https://<site>/api/method/saas_core.webhooks.receive?gateway=<name>` (POST). Built-ins also keep `/api/method/saas_core.webhooks.razorpay` and `.stripe`.

## Idempotency

- `create_checkout_session`: the API layer stores the response under the caller's `Idempotency-Key`. Add a deterministic provider reference (tenant + plan) where the provider supports one.
- `refund_payment`: always pass `receipt` through to the provider (idempotency key or reference). Billing sets `receipt` to the Refund document name and also sends `notes["refund_doc"]`. Retry rule used by billing (`refunds.issue`): if the Refund doc was tried before, call `list_payment_refunds`, adopt the entry whose `receipt` (or `notes.refund_doc`) matches, and create only if none exists; if the lookup fails or the gateway lacks `refund_lookup`, do not create. A gateway without `refund_lookup` must therefore make `refund_payment` idempotent per `receipt` itself.
- `cancel_subscription`, `ensure_captured`: safe to call twice.
- `parse_event`: deterministic `event_id` (see above).

## Errors and logging

- Raise `GatewayError(message, status=None, code="", description="")` for every provider failure; never let a raw `urllib`/`requests` exception, `KeyError` or `AttributeError` escape for expected provider conditions.
- Messages must not contain `key_id`, `key_secret`, `webhook_secret`, tokens, signatures, card data or full request/response bodies. `UrllibHttp` already trims provider errors to a code and a 300-char description.
- Never `print` or log secrets, raw webhook bodies or signatures. Use `frappe.log_error("short fixed message", "title")`.
- `client_payload` and `CheckoutResult.raw` reach the browser or the database: public values only.

## Experimental gating

`saas_core.experimental.FEATURES` lists gated features. Built-in gating: `stripe` needs `stripe`, `fake` needs
`fake_gateway`. A plugin can opt in by setting `experimental_feature = "<feature>"` on its class (pick an existing
feature name; adding a new one means editing `saas_core/experimental.py`). An unknown name is refused when the plugin
loads, with a validation error listing the known names.

A refund retry on a gateway that declares `refund` but not `refund_lookup` is marked non-retryable at once and needs
manual reconciliation, so declare `refund_lookup` whenever you declare `refund`.
