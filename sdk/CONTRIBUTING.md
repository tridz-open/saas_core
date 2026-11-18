# Contributing

frappe-saaskit-sdk is early alpha software and is not currently accepting pull requests or outside contributions. Pull requests will be closed without review.

**Issues are welcome.** If you find a bug or a problem, please [open a GitHub issue](https://github.com/tridz-open/frappe-saaskit-sdk/issues/new/choose).

## What to include in a bug report

- The version or release tag you are using (package name and version, e.g. `@frappe-saaskit/client 0.1.0-alpha.3`).
- Your environment: OS, runtime version, and how you installed it.
- What you did (exact commands or code), what you expected, and what happened instead.
- The full error output or stack trace.
- A minimal reproduction if you can make one.
- **Remove secrets and account data first**: tokens, session cookies, API keys, emails, hostnames, team ids.

## Security issues

Do not file security problems as public issues. See [SECURITY.md](SECURITY.md).

## Use at your own risk

Alpha software, provided "as is" without warranty of any kind (see [LICENSE](LICENSE)).

## Development setup (for reference, and for your own fork)


### Setup

```bash
corepack enable            # provides the pnpm version pinned in package.json (pnpm 10; works on Node 20 and 22, pnpm 11 needs Node 22.13+)
pnpm install
pnpm build && pnpm typecheck && pnpm test
```

Node 20 or newer. No backend is needed: tests use `@frappe-saaskit/mock`. `pnpm-workspace.yaml` already
allows the install scripts of `esbuild` and `msw`; if your pnpm still reports `ERR_PNPM_IGNORED_BUILDS`,
run `pnpm approve-builds`.

### Contract and codegen

Types in `packages/client/src/generated/` come from `spec/v1.yaml`, a vendored copy of saas_core's
OpenAPI contract (see `spec/SOURCE.md`). To update it:

```bash
pnpm spec:sync ../saas_core    # path to a saas_core checkout
pnpm codegen
```

`pnpm codegen:check` (run in CI) fails when generated types and the spec disagree, or when the spec is missing.

### Before opening a PR

- `pnpm build && pnpm typecheck && pnpm test && pnpm lint:internal`
- `pnpm changeset` for any user-facing change.
- Opt-in contract test against a real saas_core: `SAASCORE_URL=https://site pnpm test:contract`.
