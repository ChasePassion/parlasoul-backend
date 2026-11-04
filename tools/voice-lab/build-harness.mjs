#!/usr/bin/env node
// Bundles the probe page, resolving the app's `@/` alias while swapping the
// realtime API module for the harness shim (no app shell / better-auth needed).
import { createRequire } from "node:module";
import { existsSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const backendRoot = resolve(here, "..", "..");
// esbuild is installed under the gitignored scratch dir, not in the repo.
const toolsRequire = createRequire(`${resolve(backendRoot, "tmp", "voice-lab", "tools")}/`);
const { build } = toolsRequire("esbuild");
const frontendSrc = resolve(backendRoot, "..", "parlasoul-frontend", "src");
const shim = resolve(here, "harness", "api-shim.ts");
const entry = resolve(here, "harness", "page.ts");
const outfile = resolve(here, "harness-dist", "page.js");

const exactShimPlugin = {
  name: "realtime-api-shim",
  setup(buildApi) {
    const resolveModule = (specifier) => {
      const base = resolve(frontendSrc, specifier.slice(2));
      const candidates = [base + ".ts", base + ".tsx", `${base}/index.ts`, `${base}/index.tsx`];
      return candidates.find((candidate) => existsSync(candidate) && statSync(candidate).isFile()) ?? base;
    };
    buildApi.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: shim }));
    buildApi.onResolve({ filter: /^@\// }, (args) => ({ path: resolveModule(args.path) }));
  },
};

await build({
  entryPoints: [entry],
  bundle: true,
  format: "esm",
  target: "chrome120",
  outfile,
  logLevel: "info",
  plugins: [exactShimPlugin],
  external: [],
});

console.log(`built ${outfile}`);
