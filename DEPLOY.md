# Deploying saas_core to a Frappe Cloud control plane

There is no deploy script: the steps below are run by hand (or from your own automation) with `fcctl` and the
whitelisted `admin.ops.run` endpoint. See [docs/STATUS.md](docs/STATUS.md) for what has been verified live.

## Prerequisites
- A Frappe Cloud control-plane server and site; `fcctl` logged in (`FCCTL_TEAM`, `FCCTL_KEY`).
- Apps reachable from Frappe Cloud (connect the GitHub app/PAT in the FC dashboard for private repos).
- Local tests green (`pytest tests`) and a control-plane site backup: `fcctl -s <cp> backup create <site>`.
- Secrets (never committed): Razorpay test key id/secret/webhook secret (`rzp_test_*`; live keys are refused unless allowed) and SMTP credentials.

## Steps
| # | Step | How |
|---|---|---|
| 1 | Add apps (fcctl, saas_core, plus your recipe app if any, in this order) | `fcctl -s <cp> app add --repo URL --branch main --wait` |
| 2 | Deploy | No separate fcctl command; `app add` rebuilds the bench, `--wait` follows the task |
| 3 | Install on site | `fcctl app install saas_core --site <site>` (then your recipe app; fcctl needs no site install) |
| 4 | Site config | `fcctl site config set <site> KEY=VALUE...`: `saas_core_signup_mode`, `saas_core_console_url`, `saas_core_fake_fcctl=0`, `saas_core_default_gateway`, `saas_core_razorpay_key_id/key_secret/webhook_secret`, `saas_smtp_*` |
| 5 | Gateway bootstrap | `POST /api/method/saas_core.api.v1.admin.ops.run {"op":"bootstrap_gateway"}` (System Manager); then blank the `saas_core_razorpay_key_secret` / `_webhook_secret` site_config keys (fcctl has no unset). Manual fallback: `bench execute saas_core.gateways.bootstrap.from_site_config` |
| 6 | Email account | ops `setup_email` (reads `saas_smtp_*`); manual fallback `bench execute saas_core.scripts.setup_email_account.setup` |
| 7 | Migrate | `fcctl site migrate <site> --wait` |
| 8 | Plans | ops `sync_plans`; manual fallback `bench execute saas_core.gateways.razorpay_sync.sync_plans` |
| 9 | Verify | `/api/method/ping` returns pong; ops `fc_check` |

fcctl cannot run site methods, so steps 5/6/8 call the whitelisted `admin.ops.run` endpoint (ops: `bootstrap_gateway`, `setup_email`, `sync_plans`, `fc_check`; System Manager / SaaS Admin only; each run audited by op name; result secret-free). Use header `Authorization: token <api_key>:<api_secret>`.

## Real Frappe Cloud mode (site_config keys)
With neither `saas_core_local_bench` nor `saas_core_fake_fcctl` set, `runner.get_port()` returns `FcctlAdapter` over a real `fcctl.Client` built by `saas_core/provisioning/fc_client.py`. The FC login session is read from the DB (Platform Session, encrypted with `saas_core_session_key`), not `~/.fcctl`; expired sessions are renewed through `saas_core_fc_reauther`. Dummy values below:

| Key | Example | Purpose |
|---|---|---|
| `saas_core_fc_team` | `TEAM-00000` | FC team (required) |
| `saas_core_fc_server` | `server-abc123` | default server; a template manifest `server` overrides it |
| `saas_core_session_key` | `<Fernet key>` | encrypts the stored FC session (required) |
| `saas_core_fc_reauther` | `saas_core.fc_reauth.imap_reauther` | unattended re-login |
| `saas_core_fc_email` | `bot@example.com` | FC login email |
| `saas_core_fc_imap_host` / `_user` / `_password` | `imap.example.com` / `bot@example.com` / `<password>` | mailbox that receives the sign-in code |
| `saas_core_alert_email` | `ops@example.com` | alerted when the session is lost and sites are paused |
| `saas_core_fc_journal_dir` | `/path/private/fcctl/journal` | optional; default `<site>/private/fcctl/journal` |

Missing `saas_core_fc_team` or `saas_core_session_key` fails with a clear error naming the key. Check the wiring (SaaS Admin only, secret-free) with `POST /api/method/saas_core.api.v1.admin.ops.run {"op":"fc_check"}` -> `{authenticated, email, team, server, wildcard_domains}`. Terminate stays gated by the two-person approval and fcctl's guarded drop.

Templates (apps, backup source, hooks, health checks, Pilot gotchas): see [docs/TEMPLATES.md](docs/TEMPLATES.md). `fc_check` also returns `template_warnings`.

## Unattended FC re-login (verified: used live for FC sign-in)
When the FC session expires, keepalive calls the site_config callable `saas_core_fc_reauther`. Enable the IMAP-OTP implementation with:
`fcctl -s <cp> site config set <site> saas_core_fc_reauther=saas_core.fc_reauth.imap_reauther saas_core_fc_email=<FC login email> saas_core_fc_imap_host=<imap host> saas_core_fc_imap_user=<mailbox user> saas_core_fc_imap_password=<mailbox password>`
The mailbox must receive the Frappe Cloud sign-in code. One attempt per 10 min (redis cooldown, under a lock so workers do not trigger several OTP emails); each attempt is audited as `fc.reauth` with the outcome only. On failure it returns None and the pause + banner path applies.

## Beta access
Whitelist demo users and invite them to the hosted product. Signup has four modes (`closed` default, `invite`, `allowlist`, `open`). For a beta use `allowlist`: only emails or `@domain` patterns in Platform Settings `signup_allow` can sign up, and checkout additionally needs a verified email. The portal shows "Get started" (public config `public_signup: true`, `signup_mode: "allowlist"`); an email that is not listed gets the `not_allowlisted` error.

Switch the mode (the real key is the site_config key `saas_core_signup_mode`, read by `get_setting("signup_mode")`; site_config wins over the Platform Settings field):
```
bench --site <site> set-config saas_core_signup_mode allowlist
# Frappe Cloud: fcctl site config set <site> saas_core_signup_mode=allowlist
```
Manage the list from a shell (`bench execute` runs as Administrator; `--kwargs` is JSON):
```
bench --site <site> execute saas_core.beta_access.add_beta_users --kwargs '{"emails": ["a@x.com", "@partner.com"], "send_invite": true, "note": "demo batch 1"}'
bench --site <site> execute saas_core.beta_access.list_beta_users
bench --site <site> execute saas_core.beta_access.resend_beta_invite --kwargs '{"email": "a@x.com"}'
bench --site <site> execute saas_core.beta_access.remove_beta_users --kwargs '{"emails": ["a@x.com"]}'
```
`send_invite` emails exact addresses only (not `@domain`) the branded "You're invited to try <brand>" mail (Email Template `saas-beta-invite`, override with `saas_core_beta_invite_email_template`) linking to `<console_url>/signup?email=<email>`. The same actions are available to System Managers in the portal at `/console/admin/beta-access`, backed by `admin.beta_access.list|add|remove|resend`.

Deploy: `bench --site <site> migrate` (adds `Signup Allow.invited_at` and imports the new email template fixture), then set the mode. Closing the beta again: `set-config saas_core_signup_mode closed`.

## Known caveats
- `fcctl site config set` puts values on argv: run only from a trusted machine. Pilot must accept arbitrary config keys; fallback is Platform Settings.
- `saas_core_razorpay_*` keys are sensitive: remove after step 5.
- Order matters: steps 7 and 8 come after installs; sync_plans needs the bootstrapped Gateway Account.

## Rollback
Backup before step 3; redeploy the previous bench release for failed builds; `saas_core_signup_mode=closed` or `Gateway Account.enabled=0` stops new checkouts. Next: D6 (register the Razorpay webhook at `https://<site>/api/method/saas_core.webhooks.razorpay`).

- `saas_core_site_domain` (optional): domain tenant sites are created under. Unset: tenant sites use the domain Frappe Cloud gives (its first wildcard domain), and the real host is stored on the Tenant (`fc_site`).

## Local real-site mode (EXPERIMENTAL, dev only; needs saas_core_experimental=local_bench)
Runs the full flow against REAL Frappe sites on the same bench as the control plane, until Frappe Cloud is live. `runner.get_port()` returns `LocalBenchPort` (`saas_core/provisioning/local_bench.py`) when `saas_core_local_bench=1` (checked before `saas_core_fake_fcctl`). Tenant sites are `<subdomain>.localhost` (resolves locally); the worker shells out to `bench` from the bench root (`frappe.utils.get_bench_path()`).

Site config keys (control-plane site):
- `saas_core_local_bench` = `1` (enables the mode)
- `saas_core_local_db_root_password` = MariaDB root password (used by new-site / restore / drop-site)
- `saas_core_local_golden_sql` = absolute path of the golden site backup `.sql.gz` (passed as `--source-sql`; falls back to new-site + `restore` if the bench lacks that option)
- `saas_core_local_golden_files` / `saas_core_local_golden_private_files` = optional public/private files tarballs of the golden backup
- `saas_core_local_tenant_port` = port of the tenant URLs (default `8004`)
- `saas_core_local_bench_bin` = optional path to the `bench` executable (default: first on PATH)
- `saas_core_site_domain` = `localhost` (so site names become `<slug>.localhost`)

Notes: the bench web server must serve `*.localhost` (no `--site` pin, `serve_default_site` off). The worker runs `bench new-site` (up to 30 min) so use the `long` queue. Passwords appear on argv of the bench subprocess only and are redacted from errors. Suspend/resume use maintenance mode; terminate uses `drop-site --force --no-backup`; plan change is a no-op; backup download links are unsupported.

### Serving tenant sites locally (important)
`bench serve` without `--site` pins every request to `default_site` from common_site_config, so tenant
`*.localhost` hosts would all hit the default site (setup links then look "invalid"). Serve tenants by Host instead:

    cd <bench>/sites && ../env/bin/python -c "import frappe.app; frappe.app.serve(port=8004, sites_path='.', no_reload=True)"

and set `saas_core_tenant_url_template=http://{site}:8004` on the control-plane site.

## Transactional email layout (logos, dark mode, support address)
- All saas-* Email Templates (and any recipe's own) are generated from one layout: `saas_core/notifications/email_layout.py`
  (`python scripts/build_email_templates.py`; the recipe vendors the same file). After upgrading run `bench --site <cp> migrate`
  so the Email Template fixtures update.
- Settings: `saas_core_logo_url` (logo for light backgrounds) and `saas_core_logo_url_dark` (logo for dark mode). Both must be **absolute https URLs** that return 200 `image/png` without auth or redirects.
- Footer contact comes from `saas_core_support_email` (rendered as `{{ support_email }}`; nothing is hard-coded in templates).

## Daily tenant backups and offsite copy (experimental)
- `saas_core.jobs.daily_tenant_backups` takes one site backup per active tenant through `fcctl backup create` (live-verified).
  A failure is written to the audit log (`backup_failed`), logged, and emailed to `saas_core_alert_email`; **the tenant is never marked failed and stays active**.
- Offsite copy to the bucket (site_config `saas_core_backup_bucket` names the bucket; unset disables it) and retention pruning are **experimental** and off by default. Enable with
  `bench --site <cp> set-config saas_core_offsite_backups 1`. They need a port with `bucket_list`, `bucket_delete` and
  `copy_backup_to_bucket`; the real Frappe Cloud adapter does not have them yet, so with the flag on the job logs
  "offsite backups not configured" and skips them.
- Retention knobs (used only when offsite is on): `saas_core_backup_keep_daily` (7), `saas_core_backup_keep_weekly` (4).
