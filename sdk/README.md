# TypeScript SDK (`@frappe-saaskit/*`)

This directory of [saas_core](https://github.com/tridz-open/saas_core) holds the browser/Node client for its API. It is versioned and released together with saas_core. It used to live in a separate `frappe-saaskit-sdk` repository.

> [!WARNING]
> **ALPHA, BLEEDING EDGE.** Current line: `0.1.0-alpha.x`. Expect breaking changes between any two releases, with no
> deprecation period. Only a small part of the API has been exercised against a live backend (see
> [Verified vs experimental](#verified-vs-experimental)); everything else is experimental. Do not build a production
> business on this yet. Not on npm: install from [GitHub release tarballs](#install).

## What is this?

A TypeScript SDK for building the **customer-facing side of a SaaS** (signup, workspaces/tenants, billing and checkout,
invoices, backups) on top of **saas_core**.

**saas_core** is a Frappe app that acts as the SaaS control plane: it provisions customer sites, tracks plans and
subscriptions, takes payments (Razorpay is the verified gateway) and exposes a versioned JSON API. saas_core is
[open source](https://github.com/tridz-open/saas_core) (`tridz-open/saas_core`). Its OpenAPI contract is [`openapi/v1.yaml`](../openapi/v1.yaml) at the repo root (currently **2.0.0-alpha.1**), and the client's types are generated from it, so you can
read the full API surface without access to the backend.

Every call goes to `<site>/api/method/saas_core.api.v1.<operation>`. You can try the SDK today without saas_core by
using the in-memory [mock](#mock-backend-local-development).

## Packages

All packages share one version (lock-stepped) and are ESM-only.

| Package | Status | What it is |
|---|---|---|
| [`@frappe-saaskit/client`](packages/client/README.md) | alpha, core verified | Framework-free typed client. Typed errors, safe retries, CSRF handling. No runtime dependencies. |
| [`@frappe-saaskit/react`](packages/react/README.md) | alpha, core verified | `SaasProvider`, TanStack Query v5 hooks, Razorpay Checkout.js helper. Peers: `react >= 18`, `@tanstack/react-query >= 5`. |
| [`@frappe-saaskit/mock`](packages/mock/README.md) | alpha | In-memory saas_core lookalike (Razorpay only) for tests, demos and UI work. `msw` only for the `./node` and `./browser` entries. |
| [`@frappe-saaskit/vue`](packages/vue/README.md) | **experimental** | Vue 3 composables. No consumers yet; tested against mocked clients only. Prints a one-time console notice. |
| [`@frappe-saaskit/node`](packages/node/README.md) | **experimental** | Helpers for Node scripts/CLIs (`createNodeClient`, `tokenFromEnv`, `loadTokenFile`). Prints a one-time console notice. |

"Core verified" means the namespaces marked Verified in the [matrix below](#verified-vs-experimental) work against a live
saas_core. The rest of each package is experimental.

## Install

Install from the public GitHub release tarballs (attached to saas_core releases named `v0.1.0-alpha.N`). Tarball versions can differ from the release tag: `R` is the saas_core release tag, `V` the SDK version. Pin the exact release you tested.

```bash
R=0.1.0-alpha.3   # saas_core release
V=0.1.0-alpha.4   # SDK tarball version
B=https://github.com/tridz-open/saas_core/releases/download/v$R

# client + react; mock for development only
pnpm add "$B/frappe-saaskit-client-$V.tgz" "$B/frappe-saaskit-react-$V.tgz" \
  @tanstack/react-query react react-dom
pnpm add -D "$B/frappe-saaskit-mock-$V.tgz"
```

The other packages depend on `@frappe-saaskit/client`, which is not on the npm registry, so pin it with an override in
`package.json` (npm users: top-level `"overrides"` instead of `"pnpm": { "overrides" }`) and use the same version string:

```json
{
  "pnpm": {
    "overrides": {
      "@frappe-saaskit/client": "https://github.com/tridz-open/saas_core/releases/download/v0.1.0-alpha.3/frappe-saaskit-client-0.1.0-alpha.4.tgz"
    }
  }
}
```

Each release also has a `SHA256SUMS` asset. Bump every URL (and the override) together when you upgrade. Once the
packages are on npm, delete the override and depend on `"@frappe-saaskit/client": "^0.1.0-alpha.4"`.

Requirements: Node.js 20+, pnpm 9+ (pnpm 10 to build from source), ESM only. For real use you also need a Frappe site with
saas_core installed; for cookie auth from a browser, serve your front end from the same origin as that site (or set up
CORS with credentials yourself).

## Quickstart: client

`baseUrl` is the **site origin** (for example `https://app.example.com`), not an `/api` path; use `''` when the front end
is served from the same origin. The snippets below are typechecked in CI (see [`examples/`](examples)).

```ts
import {
  createClient, SaasError, ValidationError, ConflictError, RateLimitError,
} from '@frappe-saaskit/client';

const client = createClient({ baseUrl: 'https://app.example.com', auth: { type: 'cookie' } });

// Session: reuse an existing one, otherwise log in (the client bootstraps the CSRF token for you).
const me = await client.me.get().catch(() => client.auth.login({ email: 'me@example.com', password: '...' }));

const plans = await client.plans.list();
const slug = await client.tenants.checkSlug('acme'); // { available, reason?, suggestion? }

try {
  const { tenant, job } = await client.tenants.create({ slug: 'acme', plan: plans[0]!.code });
  // Live provisioning progress (SSE, falls back to polling when SSE is off on the site)
  const sub = client.subscribe({ jobId: job.name }, (e) => console.log(e.type, e.data));
  // later: sub.close()
} catch (e) {
  if (e instanceof ValidationError) console.log(e.fieldErrors); // 400/417/422
  else if (e instanceof ConflictError) console.log(e.message); // 409, for example slug taken
  else if (e instanceof RateLimitError) console.log(e.retryAfterMs);
  else if (e instanceof SaasError) console.log(e.status, e.code, e.requestId);
  else throw e;
}
```

Billing (day-based; preview first, then apply):

```ts
const preview = await client.billing.previewCancel(tenantId); // days used, refund_minor, stops_at, can_cancel
if (preview.can_cancel) await client.billing.cancel(tenantId, { reason: 'switching tools' });
await client.billing.previewChange(tenantId, 'pro');          // then apply with client.tenants.changePlan
await client.paymentMethods.portalLink({ tenant: tenantId });  // gateway portal to update cards
```

Auth modes:

| `auth` | Sends | Use for |
|---|---|---|
| `{ type: 'cookie' }` | the Frappe `sid` cookie plus `X-Frappe-CSRF-Token` on POSTs | browser apps on the same site |
| `{ type: 'apiKey', key, header? }` | `Authorization: token <key>` (`key:secret` for Frappe user API keys), or `<header>: <key>` | servers and scripts |

There is no bearer mode: saas_core does not validate `Authorization: Bearer` for its own API tokens yet. Never put API
secrets in front-end code.

**Retries.** GETs are retried up to 3 times on network errors, 429 and 5xx. A mutating request is retried only when
saas_core honours its `Idempotency-Key` (signup, tenant create/delete/change-plan, billing cancel, backups create,
checkout); the same key is reused across attempts. Everything else (login, logout, password reset,
`admin.tenants.create`, ...) is sent once. Set `retry: { retries: 0 }` to turn retries off.

## Quickstart: React

```tsx
import { createClient } from '@frappe-saaskit/client';
import { SaasProvider, useMe, usePlans, useCheckout, useRazorpayCheckout } from '@frappe-saaskit/react';

const client = createClient({ baseUrl: '', auth: { type: 'cookie' } }); // same origin as the saas_core site

function Pricing() {
  useMe(); // loads the session and the CSRF token needed for the mutations below
  const plans = usePlans();
  const checkout = useCheckout();
  const rzp = useRazorpayCheckout();

  const buy = async (plan: string) => {
    const session = await checkout.mutateAsync({
      plan, interval: 'month', slug: 'acme',
      success_url: location.origin + '/done', cancel_url: location.href,
    });
    if (session.client_payload) await rzp.open(session); // Razorpay Checkout.js, then verifyPayment
    else if (session.url) location.href = session.url; // hosted gateways
  };

  if (plans.isLoading) return <p>Loading...</p>;
  return (
    <ul>
      {plans.data?.map((p) => (
        <li key={p.code}><button onClick={() => buy(p.code)}>{p.title ?? p.code}</button></li>
      ))}
    </ul>
  );
}

export const App = () => <SaasProvider client={client}><Pricing /></SaasProvider>;
```

`SaasProvider` mounts its own `QueryClientProvider` unless you pass `queryClient`. `useJob(jobId)` and
`useTenantLive(tenantId)` follow provisioning live; `useCancelSubscription`, `usePreviewCancel` and `usePreviewChange`
cover billing. The full hook list is in [packages/react/README.md](packages/react/README.md).

## Verified vs experimental

Verified = exercised against a live saas_core site (Razorpay is the only live-verified gateway). Experimental = covered by
unit tests against fetch stubs or the mock only, and/or **disabled by default on saas_core**.

| Area | Client namespace | Status |
|---|---|---|
| Auth and session | `auth` (login, logout, signup), `me.get` | Verified |
| Catalog and config | `plans`, `config` | Verified |
| Tenants | `tenants` (list, get, create, slug check) | Verified |
| Jobs | `jobs` | Verified |
| Checkout | `checkout.createSession` (Razorpay, `order` mode) | Verified |
| Beta access (admin) | `admin.betaAccess` (list, add, remove, resend) | Verified |
| Billing | `billing.previewCancel`, `billing.cancel` (day-based pro-rata refund via Razorpay), `billing.previewChange` + `tenants.changePlan` upgrade via payment link | **Verified live** (cancel + refund and upgrade tested on a live deployment, Razorpay test mode) |
| Billing (other) | `subscription.get`, downgrade path of `changePlan`, `checkout.createSession` subscription mode | Experimental |
| Invoices and cards | `invoices` (incl. `pdf`), `paymentMethods.portalLink` | Experimental |
| Tenant extras | `tenants.changePlan`, `openLoginLink`, `suspendStatus`, `requestDelete`; `backups` list/create | Experimental |
| Admin and profile | `admin.tenants`, `me.update`, password reset / email verification | Experimental |
| API tokens | `apiTokens` | Experimental, **off by default** (403 `experimental_disabled`) |
| Team | `team` | Experimental, **off by default** |
| Support | `support` | Experimental, **off by default** |
| Live events | `events`, `subscribe` (SSE) | Experimental, **off by default**; `subscribe` falls back to polling |
| Backup restore | `backups.restore`, `backups.downloadLink` | Experimental, **off by default** |
| Other packages | `@frappe-saaskit/vue`, `@frappe-saaskit/node` | Experimental |

"Off by default" operations are gated on the server by the site_config key `saas_core_experimental`. Until a site
operator enables a flag (`api_tokens`, `team`, `support`, `sse`, `restore`), the call fails with HTTP 403 and code
`experimental_disabled`, which the SDK throws as `ExperimentalDisabledError`.

Removed in saas_core 2.0.0-alpha (and so in the SDK): `subscription.cancel` and `subscription.portalLink` (use
`billing.cancel` / `paymentMethods.portalLink`), `tenants.duplicate`, the Paddle gateway, and the `tenant.updated` and
`invoice.created` stream events.

## Error handling

Every method rejects with a `SaasError` subclass: `AuthError` 401 (`CsrfError` for a rejected CSRF token),
`ForbiddenError` 403 (`ExperimentalDisabledError` when the code is `experimental_disabled`), `NotFoundError` 404,
`ConflictError` 409, `ValidationError` 400/417/422, `RateLimitError` 429 (with `retryAfterMs`), `ServerError` 5xx and
`NetworkError`. Each carries `status`, `code`, `fieldErrors` and `requestId`. Both saas_core error bodies
(`{ error: { code, message, ... } }`) and Frappe-native ones (`exc_type`, `_server_messages`) are mapped. A 2xx response
that is not JSON (for example an HTML login page) becomes `SaasError` with `code: 'non_json_response'`.

```ts
import { ExperimentalDisabledError } from '@frappe-saaskit/client';

try {
  await client.apiTokens.list();
} catch (e) {
  if (e instanceof ExperimentalDisabledError) console.log('this feature is switched off on the site');
  else throw e;
}
```

## Mock backend (local development)

`@frappe-saaskit/mock` is an in-memory saas_core lookalike with seeded data: user `demo@example.com`, plans
`free`/`pro`/`business`, tenant `acme`, invoices and jobs. Provisioning jobs advance on a timer and emit SSE events.
It behaves like a default saas_core site, so experimental routes return 403 `experimental_disabled` unless you opt in with
the `experimental` option (`['api_tokens', 'team', 'support', 'sse', 'restore']` or `'all'`).

```ts
import { createClient } from '@frappe-saaskit/client';
import { createMockFetch } from '@frappe-saaskit/mock';

const fetch = createMockFetch({ stepMs: 0 }); // no network, no MSW
const client = createClient({ baseUrl: 'http://localhost', auth: { type: 'cookie' }, fetch });

await client.me.get(); // logged in by default
const { job } = await client.tenants.create({ slug: 'newco', plan: 'pro' });
fetch.reset(); // back to the seed data between tests
```

For MSW use `createMockServer()` from `@frappe-saaskit/mock/node` in Node tests, or `createMockWorker()` from
`@frappe-saaskit/mock/browser` for a browser demo. Options such as `signupMode`, `admin`, `capacityExhausted` and
`forceUnauthenticated` simulate error paths. The mock is a simulation, not a contract test: code that passes against it
can still fail against a real site. See [packages/mock/README.md](packages/mock/README.md).

## Codegen and the contract

`packages/client/src/generated/schema.d.ts` is generated by [`openapi-typescript`](https://openapi-ts.dev) from
saas_core's `openapi/v1.yaml` (one copy, at the repo root; there is no vendored spec). The generated file is committed.

```bash
pnpm codegen                      # regenerate schema.d.ts from ../openapi/v1.yaml
pnpm codegen:check                # exit 1 if schema.d.ts differs from the spec
```

The package map, request lifecycle and Razorpay checkout sequence are drawn in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Known gaps

- **Bearer / personal access tokens.** saas_core issues API tokens but does not validate `Authorization: Bearer`, so the
  SDK has no bearer mode. Use cookie or Frappe `key:secret`.
- **Idempotency** is not honoured by `admin.tenants.create` and `auth.login`, so they are never retried.
- **Error envelope.** Some Frappe-native exceptions still return `exc_type`/`_server_messages`; the SDK maps both.
- **CSRF for guests** relies on `auth.me` returning a `csrf_token`; if it fails the SDK proceeds without one.
- **No public contract target.** saas_core is open source, but there is no hosted public site. The opt-in contract test
  (`SAASCORE_URL=... pnpm test:contract`) needs a reachable saas_core site that you run yourself.
- **ESM only**, no CommonJS build. The SDK only works against saas_core's v1 API.

## Contributing

```bash
git clone https://github.com/tridz-open/saas_core.git
cd saas_core/sdk
corepack enable          # provides the pinned pnpm
pnpm install
pnpm build               # tsup -> packages/*/dist
pnpm typecheck           # packages + the README examples
pnpm test                # vitest everywhere, no backend needed (uses the mock)
pnpm codegen:check       # generated types match ../openapi/v1.yaml
pnpm lint:internal       # no internal paths or agent markers in tracked files
pnpm pack:all && pnpm smoke:pack   # one .tgz per package, installed into an empty project
```

If pnpm stops with `ERR_PNPM_IGNORED_BUILDS`, run `pnpm approve-builds`. Add a changeset (`pnpm changeset`) for any
user-facing change. See [CONTRIBUTING.md](../CONTRIBUTING.md). Repo rule: the SDK talks only to the saas_core API, and
Frappe Cloud credentials or hosts must never appear in any package, test or config (`pnpm lint:fc` enforces it).

Feedback from the Frappe community is very welcome, especially on the client/hook API shape, running everything over
Frappe sessions (cookie + CSRF) and `token key:secret`, and whether parts belong upstream in frappe-ui or frappe-js-sdk.
Open an issue on the saas_core repository.

## Related

- **saas_core**: the backend Frappe app and source of the contract ([tridz-open/saas_core](https://github.com/tridz-open/saas_core)).
- **fcctl**: a Python CLI and library for Frappe Cloud (sites, apps, backups, provisioning from a template site's backup, with an approval gate for destructive operations). saas_core uses it to create customer sites ([tridz-open/fcctl](https://github.com/tridz-open/fcctl)).
- **frappe-saaskit-starter**: a starter app built on this SDK ([tridz-open/frappe-saaskit-starter](https://github.com/tridz-open/frappe-saaskit-starter)).

## License

[MIT](LICENSE), Copyright (c) 2026 Tridz Technologies.

## Use at your own risk, and contributions

**Use at your own risk:** this is early alpha software, provided "as is" without warranty of any kind (see [LICENSE](LICENSE)). Don't use it for production data you can't afford to lose.

**Contributions:** we are not accepting pull requests or outside contributions at this time. If you find a bug or a problem, please [open a GitHub issue](https://github.com/tridz-open/saas_core/issues).
