#!/usr/bin/env bash
# Builds the SDK and writes the npm tarballs to sdk/out (via `pnpm pack`, see sdk/scripts/pack-all.mjs).
# Optionally attaches the release set to a GitHub release:  scripts/release_sdk_tarballs.sh v0.1.0-alpha.3
# Release set (override with SDK_RELEASE_PACKAGES="client react mock vue node"): client react mock.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/sdk"
pnpm install --frozen-lockfile
pnpm pack:all
pnpm smoke:pack
files=()
for p in ${SDK_RELEASE_PACKAGES:-client react mock}; do
  f=$(ls out/frappe-saaskit-"$p"-*.tgz)
  files+=("$f")
done
( cd out && sha256sum "${files[@]#out/}" > SHA256SUMS 2>/dev/null || shasum -a 256 "${files[@]#out/}" > SHA256SUMS )
printf 'tarballs:\n'; printf '  %s\n' "${files[@]}"
if [ "${1:-}" != "" ]; then
  gh release upload "$1" "${files[@]}" out/SHA256SUMS --clobber
fi
