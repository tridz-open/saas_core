---
"@frappe-saaskit/client": minor
"@frappe-saaskit/react": minor
"@frappe-saaskit/vue": minor
"@frappe-saaskit/node": minor
"@frappe-saaskit/mock": minor
---

Track saas_core API 2.0.0-alpha.1. Removed `tenants.duplicate`, `subscription.cancel` and `subscription.portalLink` (use `billing.cancel` and `paymentMethods.portalLink`); added `billing.previewCancel`, `billing.cancel` and `billing.previewChange` (React `usePreviewCancel` / `usePreviewChange`, `useCancelSubscription` now calls `billing.cancel`). Dropped the Paddle gateway and the `tenant.updated` / `invoice.created` stream events (`{ tenantId }` subscriptions now end on terminal job events or the polling fallback). New `ExperimentalDisabledError` for 403 `experimental_disabled`; `apiTokens`, `team`, `support`, SSE, `backups.restore` and `backups.downloadLink` are experimental and off by default on the server. The mock matches (404 for removed routes, `experimental` option). README rewritten for first-time readers, with typechecked examples.
