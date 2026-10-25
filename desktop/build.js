/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 * 
 * DISCLAIMER: Users must comply with applicable AI regulations.
 * Non-compliance may result in service termination or legal liability.
 */

/**
 * Encre Agent Desktop build script.
 *
 * Uses esbuild to bundle the three entry points (main process, preload script
 * and React renderer) into the `dist`/`renderer` output folders, then copies
 * third-party assets (Monaco editor, xterm.css) and generates the tray icon.
 * Run with `node desktop/build.js` (or the package.json build script).
 */

const esbuild = require("esbuild");
const path = require("path");
const fs = require("fs");

// Absolute path of this script's directory (the desktop package root).
const desktop = __dirname;

// ---------------------------------------------------------------------------
// Sync the Rust native extension into the Python source tree.
//
// `npm start` runs the unpackaged app whose backend is the in-repo Python
// source (`python -m encre.server.app`); that backend imports the compiled
// Rust extension from harness/encre/_native.<pyd|so>.  Copy the freshest
// cargo release artifact over BEFORE the frontend build so a dev run can
// never start with a stale (or missing) native module -- the extension is a
// mandatory runtime component (all permission decisions live in Rust).  When
// no release artifact exists yet, build it with cargo first.
// ---------------------------------------------------------------------------
const repoRoot = path.resolve(desktop, "..");
const nativeReleaseDir = path.join(repoRoot, "native", "target", "release");
const harnessPkg = path.join(repoRoot, "harness", "encre");
const isWin = process.platform === "win32";
const nativeExt = isWin ? ".pyd" : ".so";
const artifactExts = [".dll", ".pyd", ".so", ".dylib"];

function findNativeArtifact() {
  if (!fs.existsSync(nativeReleaseDir)) return null;
  for (const name of fs.readdirSync(nativeReleaseDir)) {
    // Skip auxiliary files (.d/.exp/.lib/.pdb); accept the cdylib itself.
    // Cargo prefixes Unix cdylib output with "lib" (lib_native.so /
    // lib_native.dylib) but leaves Windows bare (_native.dll).
    if (!name.toLowerCase().includes("native")) continue;
    if (!artifactExts.includes(path.extname(name).toLowerCase())) continue;
    const stem = path.basename(name, path.extname(name));
    if (stem === "_native" || stem === "lib_native") {
      return path.join(nativeReleaseDir, name);
    }
  }
  return null;
}

let nativeArtifact = findNativeArtifact();
if (!nativeArtifact) {
  console.log("native: no release artifact -> cargo build --release -p encre-py ...");
  const { execSync } = require("child_process");
  execSync("cargo build --release -p encre-py", {
    cwd: path.join(repoRoot, "native"),
    stdio: "inherit",
  });
  nativeArtifact = findNativeArtifact();
}
if (!nativeArtifact) {
  console.error(
    "FATAL: native extension not found in native/target/release after cargo build; " +
    "refusing to build a native-less app (permission engine lives in Rust)."
  );
  process.exit(1);
}
const nativeDst = path.join(harnessPkg, `_native${nativeExt}`);
try {
  fs.copyFileSync(nativeArtifact, nativeDst);
  console.log(
    `native: synced ${path.basename(nativeArtifact)} -> harness/encre/_native${nativeExt}`
  );
} catch (err) {
  console.error(
    `FATAL: could not sync native extension (${err.message}). ` +
    "Close any running Encre instance so the module is unlocked, then retry."
  );
  process.exit(1);
}

// Locale filter passed from build.py via LOCALES env var (comma-separated).
// When empty or unset, all locales are included.
const localeEnv = process.env.LOCALES || "";
const buildLocales = localeEnv
  ? localeEnv.split(",").map((s) => s.trim()).filter(Boolean)
  : ["zh", "en", "zh-Hant", "ja", "ko", "de", "es", "pt", "tr", "ar", "he"];

// Bundle the Electron main process (keeps electron/node-pty external).
// Main process
esbuild.buildSync({
  entryPoints: [path.join(desktop, "main.ts")],
  bundle: true,
  platform: "node",
  target: "node20",
  outfile: path.join(desktop, "dist", "main.js"),
  external: ["electron", "node-pty", "ws"],
});

// Bundle the preload script (electron stays external for security).
// Preload
esbuild.buildSync({
  entryPoints: [path.join(desktop, "preload.ts")],
  bundle: true,
  platform: "node",
  target: "node20",
  outfile: path.join(desktop, "dist", "preload.js"),
  external: ["electron"],
});

// Bundle the React renderer (browser target, automatic JSX runtime).
// - format "esm" + splitting: dynamic import() of heavy on-demand libraries
//   (xlsx/mammoth/pptx/xterm/chart.js, plus the lazy locale tables and the
//   Material file-icon set) is split into separate chunk files that are only
//   fetched when the feature is first used.
// - minify: shrinks the entry bundle by ~55% (6.7MB -> ~3MB before
//   splitting; much smaller after heavy libs move to lazy chunks).
// - define:BUILD_LOCALES — compile-time locale list; only matched locales
//   are kept in the bundle via tree-shaking.
// esbuild only ever *adds* hashed chunk files, so every rebuild used to leave
// the previous build's chunks behind (62 of the 72 files on disk were dead
// duplicates of 6 libraries). Clear the directory first so the renderer only
// ships chunks the current entry actually references.
const chunksDir = path.join(desktop, "renderer", "chunks");
if (fs.existsSync(chunksDir)) {
  fs.rmSync(chunksDir, { recursive: true, force: true });
  console.log("chunks/ cleared.");
}
const rendererResult = esbuild.buildSync({
  entryPoints: [path.join(desktop, "renderer", "src", "app.ts")],
  bundle: true,
  platform: "browser",
  target: "es2022",
  outdir: path.join(desktop, "renderer"),
  entryNames: "bundle",
  chunkNames: "chunks/[name]-[hash]",
  jsx: "automatic",
  jsxImportSource: "react",
  format: "esm",
  splitting: true,
  minify: true,
  legalComments: "inline",
  metafile: true,
  define: {
    BUILD_LOCALES_LIST: JSON.stringify(buildLocales),
  },
});

// Inject <link rel="modulepreload"> tags for the entry bundle and every chunk
// it STATICALLY imports (metafile "import-statement" kind only -- lazy
// dynamic-import chunks must not be preloaded). This overlaps the bundle's
// disk reads / antivirus scans with the stylesheet/lucide work in <head>
// instead of queueing behind the full DOM parse. Idempotent: the tag block is
// rebuilt between the modulepreload:start/end markers on every build.
const MARK_START = "<!--modulepreload:start-->";
const MARK_END = "<!--modulepreload:end-->";
try {
  const indexHtmlPath = path.join(desktop, "renderer", "index.html");
  const outputs = (rendererResult.metafile && rendererResult.metafile.outputs) || {};
  const entry = Object.values(outputs).find(
    (o) => o.entryPoint && o.entryPoint.replace(/\\/g, "/").endsWith("renderer/src/app.ts")
  );
  if (!entry) {
    console.warn("modulepreload: entry output not found in metafile; skipped.");
  } else {
    const hrefs = ["bundle.js"];
    for (const imp of entry.imports || []) {
      if (imp.kind !== "import-statement") continue;
      const p = String(imp.path).replace(/\\/g, "/");
      if (!p.endsWith(".js")) continue;
      if (p.includes("/chunks/")) hrefs.push("chunks/" + path.basename(p));
    }
    const block = hrefs.map((h) => `  <link rel="modulepreload" href="${h}" />`).join("\n");
    let html = fs.readFileSync(indexHtmlPath, "utf8");
    const s = html.indexOf(MARK_START);
    const e = html.indexOf(MARK_END);
    if (s !== -1 && e !== -1 && s < e) {
      html = html.slice(0, s + MARK_START.length) + "\n" + block + "\n  " + html.slice(e);
      fs.writeFileSync(indexHtmlPath, html);
      console.log(`modulepreload: injected ${hrefs.length} tag(s).`);
    } else {
      console.warn("modulepreload: markers missing in renderer/index.html; skipped.");
    }
  }
} catch (err) {
  console.warn("modulepreload: injection failed:", err && err.message);
}

// Copy xterm CSS
const xtermCssSrc = path.join(desktop, "node_modules", "@xterm", "xterm", "css", "xterm.css");
const xtermCssDst = path.join(desktop, "renderer", "xterm.css");
if (fs.existsSync(xtermCssSrc)) {
  fs.copyFileSync(xtermCssSrc, xtermCssDst);
  console.log("xterm.css copied.");
}

// Copy codicon CSS + fonts
const codiconCss = path.join(desktop, "node_modules", "@vscode", "codicons", "dist", "codicon.css");
const codiconCssDst = path.join(desktop, "renderer", "codicon.css");
if (fs.existsSync(codiconCss)) {
  fs.copyFileSync(codiconCss, codiconCssDst);
  console.log("codicon.css copied.");
}
const codiconFont = path.join(desktop, "node_modules", "@vscode", "codicons", "dist", "codicon.ttf");
const codiconFontDst = path.join(desktop, "renderer", "codicon.ttf");
if (fs.existsSync(codiconFont)) {
  fs.copyFileSync(codiconFont, codiconFontDst);
  console.log("codicon.ttf copied.");
}

// Copy Monaco Editor
const monacoVs = path.join(desktop, "node_modules", "monaco-editor", "min", "vs");
const monacoVsDst = path.join(desktop, "renderer", "monaco", "vs");
if (fs.existsSync(monacoVs)) {
  copyRecursive(monacoVs, monacoVsDst);
  console.log("monaco editor copied.");
}

/**
 * Recursively copies a directory tree from `src` to `dst`.
 * @param {string} src - Source directory.
 * @param {string} dst - Destination directory (created if missing).
 */
function copyRecursive(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  const entries = fs.readdirSync(src, { withFileTypes: true });
  for (const entry of entries) {
    const srcPath = path.join(src, entry.name);
    const dstPath = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      copyRecursive(srcPath, dstPath);
    } else {
      fs.copyFileSync(srcPath, dstPath);
    }
  }
}

// Generate tray icon
require("./gen_icon.js");

console.log("Build complete.");
