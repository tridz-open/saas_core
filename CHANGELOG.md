# Changelog

## 0.1.0a1 (alpha, API 2.0.0-alpha.1)

First public alpha. Expect breaking changes.

- Signup, login, password reset and a four-mode signup gate; beta-access allowlist with branded invites.
- Plans, templates and recipes; provisioning of tenant sites on Frappe Cloud via `fcctl` from a golden backup.
- Razorpay order-mode checkout and webhooks (verified, test mode); monthly-in-advance billing, whole-day IST pro-rata cancel with refund, plan upgrade.
- Branded transactional email; admin API; OpenAPI v1 (`openapi/v1.yaml`).
- Unverified features are experimental and off by default behind `saas_core_experimental` (see `docs/STATUS.md`).
- Removed: `tenants.duplicate`, Paddle gateway, `subscription.cancel`, `subscription.portal_link`.
