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
 * Renderer-only build helper.
 *
 * Mirrors the renderer section of build.js (esbuild bundle + chunk cleanup +
 * modulepreload injection) but SKIPS the native-extension sync step, which
 * fails while a running Encre instance holds `harness/encre/_native.pyd`.
 * Use this to ship renderer fixes without closing the app; the full
 * `npm run build` is still required for a native/main-process release.
 *
 * Usage: node scripts/render-build.js [--locales zh,en,...]
 */

const esbuild = require("esbuild");
const path = require("path");
const fs = require("fs");

const desktop = path.resolve(__dirname, "..");

// Locale filter (mirrors build.js; --locales overrides the default list).
const argvLocales = process.argv.find((a) => a.startsWith("--locales"));
const buildLocales = argvLocales
  ? argvLocales.split("=")[1].split(",").map((s) => s.trim()).filter(Boolean)
  : ["zh", "en", "zh-Hant", "ja", "ko", "de", "es", "pt", "tr", "ar", "he"];

// Clear dead hashed chunks so the renderer only ships what the entry
// references (same rationale as build.js).
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

// Re-inject modulepreload tags between the markers (idempotent, same logic
// as build.js).
const MARK_START = "<!--modulepreload:start-->";
const MARK_END = "<!--modulepreload:end-->";
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
  const html = fs.readFileSync(indexHtmlPath, "utf8");
  const s = html.indexOf(MARK_START);
  const e = html.indexOf(MARK_END);
  if (s !== -1 && e !== -1 && s < e) {
    fs.writeFileSync(
      indexHtmlPath,
      html.slice(0, s + MARK_START.length) + "\n" + block + "\n  " + html.slice(e)
    );
    console.log(`modulepreload: injected ${hrefs.length} tag(s).`);
  } else {
    console.warn("modulepreload: markers missing in renderer/index.html; skipped.");
  }
}

console.log("Renderer build complete.");
