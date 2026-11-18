#!/usr/bin/env node
// Usage: node scripts/spec-sync.mjs <path-to-saas_core-checkout>
// Copies openapi/v1.yaml into spec/ and rewrites spec/SOURCE.md. Then run `pnpm codegen`.
import { copyFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dir = process.argv[2];
if (!dir) {
  console.error("usage: pnpm spec:sync <path-to-saas_core-checkout>");
  process.exit(1);
}
const src = resolve(dir, "openapi/v1.yaml");
if (!existsSync(src)) {
  console.error(`spec:sync: ${src} not found`);
  process.exit(1);
}
const dest = resolve(root, "spec/v1.yaml");
copyFileSync(src, dest);
const text = readFileSync(dest, "utf8");
const version = /^info:[\s\S]*?^  version:\s*["']?([^\s"']+)/m.exec(text)?.[1] ?? "unknown";
const sha = createHash("sha256").update(readFileSync(dest)).digest("hex");
let commit = "unknown";
try {
  commit = execFileSync("git", ["-C", resolve(dir), "log", "-1", "--format=%h", "--", "openapi/v1.yaml"], { encoding: "utf8" }).trim() || commit;
} catch {}
writeFileSync(
  resolve(root, "spec/SOURCE.md"),
  `# Vendored OpenAPI contract

- Source: the saas_core backend (open source: https://github.com/tridz-open/saas_core)
- Path: \`openapi/v1.yaml\`
- Commit: ${commit}
- \`info.version\`: ${version}
- sha256: ${sha}

Refresh with \`pnpm spec:sync <path-to-saas_core-checkout>\` (copies the file, rewrites this note), then \`pnpm codegen\` and commit.
\`pnpm codegen:check\` fails if the generated types drift from this file.
`
);
console.log(`spec:sync: ${version} (${commit}) sha256 ${sha}\nNext: pnpm codegen`);
