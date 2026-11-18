# Alpha status: verified, experimental, removed

saas_core is an **alpha**. Only what has been exercised against a real deployment (Razorpay test mode, Frappe Cloud,
real SMTP) is called *verified*. Everything else is *experimental*: it is unit-tested against stubs but has never run
live, and the risky parts are **off by default** behind one setting. Removed items were deleted rather than shipped untested.

## Enabling experimental features

Set a comma-separated list (or `all`) in site_config:

```bash
bench --site <site> set-config saas_core_experimental "stripe,dunning"
bench --site <site> set-config saas_core_experimental all          # everything, for evaluation only
bench --site <site> set-config saas_core_experimental_restore 1     # or one key per feature
```

A gated API endpoint answers `403` with error code `experimental_disabled`. Gated background jobs return without doing anything (the gated restore job fails with a message).
OpenAPI marks gated operations with `x-experimental` and an `[EXPERIMENTAL ...]` summary.

| Feature name | What it unlocks |
|---|---|
| `stripe` | Stripe gateway and `/webhooks.stripe` (no refunds, so cancel-with-refund fails; built from API docs) |
| `fake_gateway` | In-memory `fake` reference gateway for tests and dev (never charges; refused in live mode). See [gateways/README.md](gateways/README.md) |
| `razorpay_subscription` | Razorpay `checkout_mode=subscription` (default and verified mode is `order`) |
| `dunning` | Staged failed-payment dunning, auto suspend/resume (`dunning.run_daily`) |
| `expiry_sweep` | Daily expired-subscription sweep |
| `invoice_renewals` | Invoice-mode renewals via Razorpay payment links (`issue_due_invoices`) |
| `restore` | `tenants.backups.restore` / `download_link`, `restore` job, bucket-upload restore (guessed Pilot `/uploads` routes) |
| `trust_tier_preflight` | FC trust-tier capacity preflight before provisioning |
| `local_bench` | Local bench provisioning mode (`saas_core_local_bench`, dev only) |
| `sse` | `events.stream` Server-Sent Events |
| `api_tokens` | `api_tokens.list/create` (revoke always works) |
| `team` | `tenants.team.*` and `team.accept_invite` |
| `support` | `support.tickets.*` |

Not gated but still unverified (labelled here and in OpenAPI only, because the verified cancel and upgrade flows
share their code): suspend / resume / terminate provision jobs, the downgrade path of `tenants.change_plan`
(partial refund, scheduled switch) and the FC-side server resize that `change_plan` performs.

## Matrix

| Area | Status | Notes |
|---|---|---|
| Signup, login, logout, session, password reset | Verified | |
| Beta-access allowlist and invites (`admin.beta_access.*`) | Verified | |
| Branded transactional email over SMTP | Verified | Tenant-setup and beta-invite mails live; billing mails (renewal receipt, refund, cancellation) are template-rendered in tests only |
| Razorpay **order**-mode checkout + payment capture, `checkout.verify_payment` | Verified | `capture_authorized` path included |
| Razorpay webhooks, incl. `payment_link.paid` | Verified | |
| Provisioning on Frappe Cloud from a golden backup (`fcctl` workflow, hooks, release, health) | Verified | Uses `BackupSource` `from_backup` |
| Cancel with day-based pro-rata refund (`billing.preview_cancel`, `billing.cancel`) | Verified | The only cancel path |
| Plan **upgrade** (`billing.preview_change`, payment link) | Verified | |
| Template validation (`templates.list`, `provisioning/templates.py`) | Verified | |
| Legacy subscription backfill (`billing.backfill`, post_model_sync patch) | Verified | No-op on a fresh install |
| Admin ops `fc_check` | Verified | `bootstrap_gateway`, `setup_email`, `sync_plans` used by the deploy runbook, not covered by a live spec |
| IMAP re-login for Frappe Cloud (`fc_reauth.imap_reauther`) | Verified | Used live for unattended FC sign-in |
| Daily tenant backups (`fcctl backup create`) | Verified | Offsite copy / pruning is off unless `saas_core_offsite_backups` is set and the port supports buckets (the Frappe Cloud adapter does not yet) |
| Razorpay subscription mode | Experimental (`razorpay_subscription`) | |
| Dunning, expiry sweep, invoice renewals | Experimental (`dunning`, `expiry_sweep`, `invoice_renewals`) | Payment-failed email and `past_due` status still happen without `dunning` |
| Stripe | Experimental (`stripe`) | No refunds |
| Suspend / resume / terminate jobs | Experimental (labelled) | Terminate also needs the two-person approval gate |
| `tenants.change_plan` downgrade | Experimental (labelled) | |
| Backup restore, download links, bucket-upload restore | Experimental (`restore`) | Live restore was stopped by the approval gate; `download_links` returned 500 live |
| Trust-tier preflight | Experimental (`trust_tier_preflight`) | |
| Local bench mode | Experimental (`local_bench`) | Development only |
| SSE events | Experimental (`sse`) | Each stream holds a worker for up to an hour |
| API tokens | Experimental (`api_tokens`) | `Authorization: Bearer <secret>` authenticates REST calls (`saas_core.token_auth`); a bad, revoked or expired Bearer token returns 401 in the API error shape; unit-tested only, not yet verified on a live bench. Tenant-scoped tokens are limited via `tenant_access` |
| Team invites and roles | Experimental (`team`) | |
| Support tickets | Experimental (`support`) | No staff-side notification |
| `invoices.pdf` | Honest stub | Redirects to the gateway-hosted payment URL when one exists and its host is allowlisted (https only; built-in Razorpay/Stripe hosts, extend via site_config `saas_core_pdf_url_hosts`); otherwise 404 `not_available`. No PDF is generated |
| `payment_methods.portal_link` | Experimental (no gate) | Razorpay has no portal (returns `return_url`); Stripe is experimental |
| `tenants.duplicate` | **Removed** | Enqueued a job kind with no runner handler |
| Paddle gateway | **Removed** | Was a stub |
| `subscription.cancel` | **Removed** | Bypassed the refund policy; use `billing.cancel` |
| `subscription.portal_link` | **Removed** | Duplicate of `payment_methods.portal_link` |
| Event types `tenant.updated`, `invoice.created` | **Removed** | Never published |
| `razorpay` / `stripe` pip dependencies | **Removed** | Never imported; gateway HTTP is hand-rolled |
| Deploy script, internal progress notes | **Removed** | |

## site_config reference

| Key | Purpose |
|---|---|
| `saas_core_experimental` / `saas_core_experimental_<feature>` | Enable experimental features (above) |
| `saas_core_session_key` | Fernet key for the stored FC session (required for real provisioning) |
| `saas_core_fc_team`, `saas_core_fc_server` | FC team and default server (required for real provisioning) |
| `saas_core_fc_email`, `saas_core_fc_reauther`, `saas_core_fc_imap_host/_user/_password` | Unattended FC re-login |
| `saas_core_fc_dedicated_bench`, `saas_core_fc_health_attempts`, `saas_core_fc_health_delay`, `saas_core_fc_journal_dir` | FC provisioning tuning (health defaults: 12 attempts x 10 s) |
| `saas_core_fake_fcctl` | In-memory fake instead of Frappe Cloud (dev/test only) |
| `saas_core_default_gateway` | Default gateway (default: first enabled Gateway Account, else `razorpay`) |
| `saas_core_razorpay_key_id/_key_secret/_webhook_secret` | One-shot input for the `bootstrap_gateway` op; blank them afterwards |
| `saas_core_signup_mode`, `saas_core_public_signup` | Signup gate (`closed`/`invite`/`allowlist`/`open`) |
| `saas_core_capacity_open`, `saas_core_max_tenants_per_user` | Capacity switches |
| `saas_core_site_domain`, `saas_core_tenant_url_template`, `saas_core_cp_url`, `saas_core_console_url` | URLs |
| `saas_core_setup_link_expiry_hours` | Tenant setup link lifetime |
| `saas_core_setup_config_prefixes` | Extra key prefixes (comma list) written beside `saas_setup_cp_url/_secret` into tenant site_config, for tenant-side setup apps that read another name |
| `saas_core_setup_signature_header_aliases` | Extra header names (comma list) accepted for the tenant setup callback besides `X-Saas-Signature` |
| `saas_core_brand`, `_brand_color`, `_logo_url`, `_logo_url_dark`, `_support_email`, `_plan_code_prefix` | Email branding; `plan_code_prefix` is stripped from plan codes in mail |
| `saas_core_<key>_email_template` | Email Template overrides (`welcome`, `payment_failed`, `dunning`, `invoice`, `password_reset`, `admin_invite`, `beta_invite`) |
| `saas_core_alert_email` | Ops alerts (FC session lost, backup failed) |
| `saas_core_offsite_backups`, `saas_core_backup_bucket`, `saas_core_backup_keep_daily/_weekly` | Offsite backup copy and retention (experimental) |
| `saas_core_payment_banner` | Banner text for the manual (mock) gateway |
| `saas_core_local_bench`, `saas_core_local_db_root_password` (+ other `saas_core_local_*`) | Local bench mode (experimental) |
