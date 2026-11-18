# Changelog

## 0.1.0a2 (alpha, API 2.0.0-alpha.1)

- Package metadata: version bumped to `0.1.0a2`.
- Added public policy files: `SECURITY.md` (private vulnerability reporting), `CONTRIBUTING.md` (issues welcome, no pull requests), GitHub issue and pull request templates.
- README: "Use at your own risk, and contributions" section replaces "Contributing".

## 0.1.0a1 (alpha, API 2.0.0-alpha.1)

First public alpha. Expect breaking changes.

- Signup, login, password reset and a four-mode signup gate; beta-access allowlist with branded invites.
- Plans, templates and recipes; provisioning of tenant sites on Frappe Cloud via `fcctl` from a golden backup.
- Razorpay order-mode checkout and webhooks (verified, test mode); monthly-in-advance billing, whole-day IST pro-rata cancel with refund, plan upgrade.
- Branded transactional email; admin API; OpenAPI v1 (`openapi/v1.yaml`).
- Unverified features are experimental and off by default behind `saas_core_experimental` (see `docs/STATUS.md`).
- Fixed: `invoices.pdf` open redirect. Redirects now go only to https hosts on an allowlist (site_config `saas_core_pdf_url_hosts` extends it).
- Added: `Authorization: Bearer <secret>` API-token authentication via `auth_hooks`; bad tokens get 401 in the API error shape (experimental, `api_tokens`).
- Fixed: `patches.txt` now has both `[pre_model_sync]` and `[post_model_sync]` sections, as Frappe requires.
- Changed: depends on fcctl `v0.1.0-alpha.4`.
- Removed: `tenants.duplicate`, Paddle gateway, `subscription.cancel`, `subscription.portal_link`.
