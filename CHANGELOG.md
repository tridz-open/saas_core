# Changelog

## Unreleased

- Docs and messages now say "template site" (the prepared site whose backup is restored into each new tenant site) instead of "golden backup". Config keys (`saas_golden`, `saas_core_local_golden_*`) are unchanged.
- Fixed invoice renewals firing early on sites whose timezone is ahead of UTC (e.g. Asia/Kolkata): the billing processor, expiry sweep and dunning compared `period_end` (naive UTC) against Frappe's site-local `now_datetime()`, so `issue_due_invoices` issued renewals up to the UTC offset early. They now use naive UTC throughout, and `issued_at`, `paid_at`, `period_start` and `last_event_at` are stamped in UTC too. A passed-in `now` is normalised to naive UTC.
- Fixed invoice renewals firing early on sites whose timezone is ahead of UTC (e.g. Asia/Kolkata): the billing processor, expiry sweep and dunning compared `period_end` (naive UTC) against Frappe's site-local `now_datetime()`, so `issue_due_invoices` issued renewals up to the UTC offset early. They now use naive UTC throughout, and `issued_at`, `paid_at`, `period_start` and `last_event_at` are stamped in UTC too. A passed-in `now` is normalised to naive UTC. Scheduled plan changes (`Provision Job` `effective_at`, run by `run_due_scheduled`/`run_job`) now compare in naive UTC, so downgrades no longer run up to 5.5 hours early on an IST site, and the manual (mock) gateway stamps `period_end`, `last_event_at`, `issued_at` and `paid_at` in UTC. `sweep_received_events` converts a given `now` explicitly to site-local for the `modified`/`creation` cutoff. The test stub's site clock is now IST (UTC+5:30) so local/UTC mixing is caught.

## 0.1.0a3 (alpha, API 2.0.0-alpha.1)

- The TypeScript SDK (`@frappe-saaskit/client`, `react`, `mock`, plus `vue` and `node`) moved into this repository under `sdk/`, with its git history. It is now versioned and released together with saas_core; the separate `frappe-saaskit-sdk` repository is being retired.
- Single source of truth for the API description: the SDK codegen reads `openapi/v1.yaml` directly (the vendored copy and `spec:sync` are gone). `pnpm codegen:check` fails if the contract changes without regenerating.
- npm package names are unchanged. SDK version `0.1.0-alpha.4` (SDK tarballs are attached to the saas_core release `v0.1.0-alpha.3`).
- CI: new Node job (build, typecheck, test, codegen check, lint, pack smoke test; Node 20 and 22) in `sdk/`; the product-name gate now covers `sdk/`.
- Release flow documented in `docs/RELEASING.md`; `scripts/release_sdk_tarballs.sh` builds the tarballs.

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
