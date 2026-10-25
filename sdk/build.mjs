/**
 * Builds the distributable plugin UI SDK.
 *
 * Outputs (all self-contained, no runtime dependency on the desktop app):
 *   dist/index.js       ESM runtime bundle
 *   dist/index.d.ts     type declarations
 *   dist/contract.json  machine-readable control/RPC/slot contract
 *
 * esbuild is resolved from this repo's desktop install; set ENCRE_ESBUILD to
 * point at another binary when building the SDK standalone.
 */
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const dist = resolve(here, "dist");
mkdirSync(dist, { recursive: true });

// The renderer contract is canonical.  Copy it in before compiling so the
// distributed SDK can never advertise a class/RPC/slot the host does not have.
copyFileSync(
  resolve(root, "desktop/renderer/src/plugin_sdk/contract.ts"),
  resolve(here, "src/contract.ts"),
);

function loadEsbuild() {
  const candidates = [
    process.env.ENCRE_ESBUILD,
    resolve(root, "desktop/node_modules/esbuild"),
    resolve(root, "node_modules/esbuild"),
    "esbuild",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return createRequire(import.meta.url)(candidate);
    } catch {
      // Try the next candidate.
    }
  }
  throw new Error("esbuild not found; run `npm install` in desktop/ or set ENCRE_ESBUILD");
}

const esbuild = loadEsbuild();

await esbuild.build({
  entryPoints: [resolve(here, "src/index.ts")],
  outfile: resolve(dist, "index.js"),
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["es2020"],
  legalComments: "none",
});

// Contract JSON is generated from the same TypeScript contract the renderer
// compiles against, so the SDK cannot drift from the host's alias/RPC tables.
const contractCjs = resolve(dist, ".contract.cjs");
await esbuild.build({
  entryPoints: [resolve(here, "src/contract.ts")],
  outfile: contractCjs,
  bundle: true,
  format: "cjs",
  platform: "node",
  target: ["node18"],
  legalComments: "none",
});
const requireCjs = createRequire(import.meta.url);
const contract = requireCjs(contractCjs);
const json = {
  sdk_version: contract.PLUGIN_SDK_VERSION,
  contract_version: contract.PLUGIN_UI_CONTRACT_VERSION,
  classes: contract.EA_UI_CLASS_MAP,
  modules: contract.EA_UI_MODULE_NAMES,
  rpc: contract.EA_UI_RPC_NAMES,
  iframe_rpc: contract.EA_UI_IFRAME_RPC_ALLOWLIST,
  slots: contract.PLUGIN_SLOTS,
  contribution_kinds: contract.PLUGIN_CONTRIBUTION_KINDS,
};
writeFileSync(resolve(dist, "contract.json"), JSON.stringify(json, null, 2) + "\n", "utf8");
rmSync(contractCjs, { force: true });

const tsc = resolve(root, "desktop/node_modules/typescript/bin/tsc");
execFileSync(process.execPath, [tsc, "-p", resolve(here, "tsconfig.json")], { stdio: "inherit" });
copyFileSync(resolve(here, "README.md"), resolve(dist, "README.md"));

const pkg = JSON.parse(readFileSync(resolve(here, "package.json"), "utf8"));
writeFileSync(resolve(dist, "package.json"), JSON.stringify(pkg, null, 2) + "\n", "utf8");
console.log("plugin UI SDK built -> sdk/dist");
