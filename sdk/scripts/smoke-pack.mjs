#!/usr/bin/env node
// Installs the packed tarballs from ./out into an empty temp project (as SDK_DISTRIBUTION.md describes),
// imports every package, and bundles the mock root for the browser. Run `pnpm pack:all` first.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const out = resolve(root, process.argv[2] || "out");
const tgz = Object.fromEntries(
  readdirSync(out)
    .filter((f) => f.endsWith(".tgz"))
    .map((f) => [f.match(/^frappe-saaskit-([a-z]+)-/)?.[1], join(out, f)]),
);
for (const n of ["client", "react", "vue", "node", "mock"]) {
  if (!tgz[n]) {
    console.error(`smoke-pack: missing tarball for ${n} in ${out} (run pnpm pack:all)`);
    process.exit(1);
  }
}
const dir = mkdtempSync(join(tmpdir(), "saaskit-smoke-"));
const sh = (cmd, args, cwd = dir) => execFileSync(cmd, args, { cwd, stdio: "inherit" });
try {
  const spec = (n) => `file:${tgz[n]}`;
  writeFileSync(
    join(dir, "package.json"),
    JSON.stringify(
      {
        name: "smoke",
        private: true,
        type: "module",
        dependencies: {
          "@frappe-saaskit/client": spec("client"),
          "@frappe-saaskit/react": spec("react"),
          "@frappe-saaskit/vue": spec("vue"),
          "@frappe-saaskit/node": spec("node"),
          "@frappe-saaskit/mock": spec("mock"),
          "@tanstack/react-query": "^5.60.0",
          react: "^19.0.0",
          "react-dom": "^19.0.0",
          vue: "^3.5.0",
        },
        overrides: { "@frappe-saaskit/client": spec("client") },
      },
      null,
      2,
    ),
  );
  sh("npm", ["install", "--no-audit", "--no-fund", "--loglevel=error"]);
  mkdirSync(join(dir, "t"));
  writeFileSync(
    join(dir, "t/check.mjs"),
    `
import { createClient, SaasError, CsrfError } from '@frappe-saaskit/client';
import * as react from '@frappe-saaskit/react';
import * as vue from '@frappe-saaskit/vue';
import * as node from '@frappe-saaskit/node';
import { createMockFetch } from '@frappe-saaskit/mock';
import { createMockServer } from '@frappe-saaskit/mock/node';
const must = (c, m) => { if (!c) { console.error('smoke FAIL: ' + m); process.exit(1); } };
must(typeof createClient === 'function' && typeof CsrfError === 'function' && SaasError, 'client exports');
must(typeof react.SaasProvider === 'function' && typeof react.useRazorpayCheckout === 'function', 'react exports');
must(Object.keys(vue).length > 0 && Object.keys(node).length > 0, 'vue/node exports');
must(typeof createMockServer === 'function', 'mock/node export');
const c = createClient({ baseUrl: 'http://mock', fetch: createMockFetch({ baseUrl: 'http://mock' }) });
const plans = await c.plans.list();
must(Array.isArray(plans), 'mock plans.list works through the packed client');
console.log('smoke OK: all packages import; mock round-trip returned ' + plans.length + ' plans');
`,
  );
  sh("node", ["t/check.mjs"]);
  const esbuild = join(root, "node_modules/.bin/esbuild");
  sh(esbuild, ["--bundle", "--platform=browser", "--format=esm", "--external:msw", "--outfile=t/browser.js", "node_modules/@frappe-saaskit/mock/dist/index.js"]);
  console.log("smoke OK: @frappe-saaskit/mock root bundles for the browser");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
