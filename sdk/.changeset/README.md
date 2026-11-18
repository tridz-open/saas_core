# Changesets

Run `pnpm changeset` to describe a user-facing change. All `@frappe-saaskit/*` packages are version-locked
(`fixed` group), so one changeset bumps them together. Maintainers run `pnpm version-packages` and then
tag `vX.Y.Z`; the release workflow attaches the packed tarballs.
