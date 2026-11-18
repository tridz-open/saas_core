#!/usr/bin/env node
// Fails if tracked files contain internal-workspace artifacts (absolute paths, agent markers).
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const SKIP = [/generated\//, /pnpm-lock\.yaml$/, /^scripts\/lint-internal\.mjs$/, /\.(png|jpg|svg)$/];
const RULES = [
  [/\/Users\//, "absolute /Users/ path"],
  [/ury_workspaces/, "internal workspace name"],
  [/\btracks\//, "internal tracks/ path"],
  [/\bswarm\b/i, "agent swarm marker"],
  [/\bPROGRESS(\.md)?\b/, "agent progress log"],
  [/\bL[0-9]\b/, "agent level marker (L0-L9)"],
  [/TODO\(agent/, "agent TODO"],
  [/\bury\b/i, "product-specific name (URY)"],
];
const files = execFileSync("git", ["ls-files"], { encoding: "utf8" }).split("\n").filter(Boolean);
let bad = 0;
for (const f of files) {
  if (SKIP.some((r) => r.test(f))) continue;
  let text;
  try {
    text = readFileSync(f, "utf8");
  } catch {
    continue;
  }
  text.split("\n").forEach((line, i) => {
    for (const [re, why] of RULES) {
      if (re.test(line)) {
        console.error(`${f}:${i + 1}: ${why}: ${line.trim().slice(0, 100)}`);
        bad++;
      }
    }
  });
}
if (bad) {
  console.error(`lint:internal FAILED (${bad} hit${bad === 1 ? "" : "s"})`);
  process.exit(1);
}
console.log("lint:internal OK");
