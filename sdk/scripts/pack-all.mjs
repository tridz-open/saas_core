#!/usr/bin/env node
// Builds every package and writes one .tgz per package into ./out (pnpm rewrites workspace:* to exact versions).
import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const out = resolve(root, process.argv[2] || "out");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
const run = (args) => execFileSync("pnpm", args, { cwd: root, stdio: "inherit" });
run(["build"]);
run(["-r", "--filter", "./packages/*", "pack", "--pack-destination", out]);
console.log(readdirSync(out).filter((f) => f.endsWith(".tgz")).join("\n"));
