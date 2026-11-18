# @frappe-saaskit/client

## 0.1.0-alpha.3

### Minor Changes

- Track saas_core API 2.0.0-alpha.1. Removed `tenants.duplicate`, `subscription.cancel` and `subscription.portalLink` (use `billing.cancel` and `paymentMethods.portalLink`); added `billing.previewCancel`, `billing.cancel` and `billing.previewChange` (React `usePreviewCancel` / `usePreviewChange`, `useCancelSubscription` now calls `billing.cancel`). Dropped the Paddle gateway and the `tenant.updated` / `invoice.created` stream events (`{ tenantId }` subscriptions now end on terminal job events or the polling fallback). New `ExperimentalDisabledError` for 403 `experimental_disabled`; `apiTokens`, `team`, `support`, SSE, `backups.restore` and `backups.downloadLink` are experimental and off by default on the server. The mock matches (404 for removed routes, `experimental` option). README rewritten for first-time readers, with typechecked examples.
- cb8ea7a: OSS alpha cleanup. Removed the `bearer` auth strategy (saas_core rejects it) and the Node `scheme` option. The mock now supports Razorpay only (Stripe and Paddle paths removed). `@frappe-saaskit/vue` and `@frappe-saaskit/node` are marked experimental with a one-time console notice. README gains a "Verified against live saas_core" matrix.

## 0.1.0-alpha.2

### Minor Changes

- a6d8553: Portal v2 API: `Me.email_verified`, `Job.started_at` and the `JobStep.key` / `status` aliases (contract resynced to saas_core). New System Manager `admin.betaAccess` client methods: `list`, `add`, `remove` and `resend`, with typed `BetaAccess*` models.

## 0.1.0-alpha.1

### Patch Changes

- Sync vendored OpenAPI contract to saas_core 1.4.1: product-specific wording removed and the setup callback signature header is now `X-Saas-Signature`. Regenerated client types.

## 0.1.0-alpha.0

### Minor Changes

- 945b4aa: First alpha. Typed client for saas_core API v1.4.0, React/Vue bindings, Node helpers, in-memory mock.
  Retries only idempotent requests, maps Frappe-native errors, bootstraps CSRF, MIT licensed.
