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
 * Encre desktop build script.
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
esbuild.buildSync({
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
  define: {
    BUILD_LOCALES_LIST: JSON.stringify(buildLocales),
  },
});

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
