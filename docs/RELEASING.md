# Releasing

saas_core (the Frappe app) and the TypeScript SDK in `sdk/` are released together as one GitHub release.

## Versions

- Python app: `saas_core/__init__.py` (`0.1.0aN`, PEP 440). Release tag: `v0.1.0-alpha.N` (same number).
- SDK: every `sdk/packages/*/package.json` shares one version (`0.1.0-alpha.M`, lock-stepped). It may differ from `N`
  while both lines are alpha; the release notes state which SDK version a release carries.
- API contract: `openapi/v1.yaml`, regenerated from `openapi/_gen_v1.py`. After changing it run `cd sdk && pnpm codegen`
  and commit `sdk/packages/client/src/generated/schema.d.ts`; CI (`pnpm codegen:check`) fails otherwise.

## Steps

1. In a PR: bump `saas_core/__init__.py`, bump the SDK versions, and add entries to `CHANGELOG.md` and `sdk/CHANGELOG.md`.
2. Merge when CI is green (Python tests, OpenAPI diff, product-name gate, SDK jobs on Node 20 and 22).
3. Create a prerelease on the merge commit:
   `gh release create v0.1.0-alpha.N --target <merge-sha> --prerelease --title ... --notes ...`
4. Build and attach the tarballs: `scripts/release_sdk_tarballs.sh v0.1.0-alpha.N`
   (or run the `release-sdk` workflow with the tag). This runs `pnpm pack` for the packages and uploads
   `frappe-saaskit-{client,react,mock}-<sdk version>.tgz` plus `SHA256SUMS`. Set `SDK_RELEASE_PACKAGES` to include `vue`/`node`.
5. Check every download URL returns 200:
   `https://github.com/tridz-open/saas_core/releases/download/v0.1.0-alpha.N/frappe-saaskit-client-<sdk version>.tgz`

The packages are not published to npm (see `sdk/README.md#install`).
