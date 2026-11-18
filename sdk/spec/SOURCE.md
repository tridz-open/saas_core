# Vendored OpenAPI contract

- Source: the saas_core backend (open source: https://github.com/tridz-open/saas_core)
- Path: `openapi/v1.yaml`
- Commit: 9a16328
- `info.version`: 2.0.0-alpha.1
- sha256: 6c3b32fd9b040af96c1007f0a3ebf88d43a0c962153395323bf4c69c13eee552

Refresh with `pnpm spec:sync <path-to-saas_core-checkout>` (copies the file, rewrites this note), then `pnpm codegen` and commit.
`pnpm codegen:check` fails if the generated types drift from this file.
