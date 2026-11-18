import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { join, relative } from "node:path";

const root = join(__dirname, "..");
const FORBIDDEN = /frappecloud|frappe\.cloud|\.frappe\.cloud|press\.frappe|fc_token|FRAPPE_CLOUD|\bpilot\b/i;
const SKIP = new Set(["node_modules", "dist"]);

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (SKIP.has(name)) continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

function targets(): string[] {
  const files: string[] = [];
  const pkgs = join(root, "packages");
  if (!existsSync(pkgs)) return files;
  for (const pkg of readdirSync(pkgs)) {
    const d = join(pkgs, pkg);
    if (!statSync(d).isDirectory()) continue;
    const src = join(d, "src");
    if (existsSync(src)) walk(src, files);
    for (const f of ["README.md", "package.json"]) {
      if (existsSync(join(d, f))) files.push(join(d, f));
    }
  }
  return files;
}

describe("no Frappe Cloud references in the kit", () => {
  it("has no forbidden tokens in packages", () => {
    const hits: string[] = [];
    for (const f of targets()) {
      readFileSync(f, "utf8").split("\n").forEach((line, i) => {
        if (FORBIDDEN.test(line)) hits.push(`${relative(root, f)}:${i + 1}: ${line.trim()}`);
      });
    }
    if (hits.length) console.error("Forbidden references:\n" + hits.join("\n"));
    expect(hits).toEqual([]);
  });
});
