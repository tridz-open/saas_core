# Templates

A Template (doctype `Template`) is the recipe for a new tenant site: which apps, which backup to restore, which hooks to run, how to check health. It is validated by `saas_core.provisioning.templates.validate_template` on every save and again when provisioning starts. Hard errors raise; softer problems come back as `warnings` (shown with `frappe.msgprint` on save, and listed under `template_warnings` by the admin op `fc_check`).

## Fields

| Field | Type | Meaning |
|---|---|---|
| `slug` | Data | Unique id. |
| `title`, `description` | Data | Display text. |
| `is_public` | Check, **default 0** | Checkout rejects templates that are not public. Tick it for any template a public Plan references. |
| `min_plan` | Link Plan | Lowest plan allowed to use the template. |
| `recipe` | Data | Name of a registered recipe (`saas_core.recipes.registry`); supplies tenant site_config and the `name@version` hook callables. |
| `apps` | Table | Rows `{app, source_repo, branch, version, install_order}`. At least one; no duplicates. Installed in ascending `install_order` (blank = row position). |
| `backup_source` | JSON | The template site to restore from (its backup). Shapes below. Empty = fresh site. |
| `hooks` | JSON list | `[{"name": "...", "version": "1", "args": {...}}]`. `name` and `version` are required, `args` is optional. Run after restore, in order. |
| `site_admin` | Check, default 0 | Hooks receive `ctx.site_admin` (fcctl `SiteAdminClient`). Required when any recipe hook (anything not `core.*`, e.g. `acme.*`) uses it. |
| `sanitize_config` | JSON object | Passed to the post-restore sanitize step. |
| `health_checks` | JSON list | `[{"name": "login", "path": "/login", "expect": 200}]`. Dicts, not callables. See below. |
| `site_config` | JSON object | Merged into the new site's site_config. |

### backup_source shapes

```json
{"profile": "gold", "server": "srv-1", "site": "template.example.com", "ts": "20260101_000000"}
{"bucket": "acme-backups", "prefix": "templates/acme-v16/20260101_000000",
 "manifest": {"parts": [{"name": "database.sql.gz", "sha256": "..."}]}}
{"bucket": "acme-backups", "prefix": "templates/acme-v16/20260101_000000", "manifest": "templates/acme-v16/manifest.json"}
```

The first is a Frappe Cloud site backup. The bucket forms restore a template site's backup from object storage: parts are sha256-verified, and `manifest` may be an inline object or a bucket path fetched at restore time. `profile` + `site` (+ backup) or `bucket_key`/`bucket` are accepted by validation.

### Hooks

`core.*` hooks are built in to fcctl. Recipe hooks are `name@version` entries from the recipe's `hooks()` map. Every template should end with the two release hooks:

```json
[{"name": "core.scheduler_resume", "version": "1"}, {"name": "core.maintenance_off", "version": "1"}]
```

### Health checks and retries

Pilot's own site health always runs first, then each check: `{name, path, expect}` does `GET https://<site><path>` and compares the status (`expect` default 200, `path` default `/`). Entries with `"admin": true` are skipped by the HTTP runner. Each check is retried because lifting maintenance takes a few seconds to reach the web workers:

| site_config key | Default | Meaning |
|---|---|---|
| `saas_core_fc_health_attempts` | 12 | tries per check |
| `saas_core_fc_health_delay` | 10 | seconds between tries |

### Shared vs dedicated bench

By default the provisioner passes `allow_existing_sites=True`: tenant servers are shared, so many customer sites live on the same bench and the "empty bench" guard is off. Set `saas_core_fc_dedicated_bench=1` in site_config to turn the guard on (the bench must then be empty).

## Minimal working example

```json
{
  "slug": "starter",
  "title": "Starter",
  "is_public": 1,
  "site_admin": 1,
  "apps": [{"app": "frappe", "install_order": 1}, {"app": "erpnext", "install_order": 2}],
  "backup_source": {"bucket": "acme-backups", "prefix": "templates/starter/20260101_000000", "manifest": "templates/starter/manifest.json"},
  "hooks": [
    {"name": "acme.seed", "version": "1", "args": {}},
    {"name": "core.scheduler_resume", "version": "1"},
    {"name": "core.maintenance_off", "version": "1"}
  ],
  "health_checks": [{"name": "login", "path": "/login", "expect": 200}],
  "site_config": {}
}
```

## Frappe Cloud (Pilot) gotchas

These fail only at runtime, so `validate_template` checks them up front.

1. `core.set_host_name` and `core.set_fresh_encryption_key` are rejected by Pilot (protected config). Validation raises a `TemplateError`; remove the hook, Pilot manages the host name and encryption key.
2. Without the trailing `core.scheduler_resume` and `core.maintenance_off` hooks the site stays at 503. fcctl releases isolation by default from v0.1.x; add both hooks for older fcctl (validation warns).
3. Set `site_admin` when the template has recipe hooks (the `acme.*` hooks) that use `ctx.site_admin` (validation warns).
4. `is_public` defaults to 0 and checkout rejects non-public templates (validation warns when a public Plan references a non-public template).
5. Health checks are dicts (`{name, path, expect}`), not callables, and are retried per the keys above.
