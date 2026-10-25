#!/usr/bin/env node
/**
 * i18n audit -- guards the two failure modes that put raw key text on screen:
 *
 *   1. a t("a.b") call whose key exists in NO locale table.  t() falls back to
 *      returning the key itself, so the UI literally renders
 *      "app.modelProviderTooltip" / "chat.reviewPlanMain" -- and a
 *      `t(...) || "English"` guard does not help, because the raw key is
 *      truthy.  This is a hard failure.
 *
 *   2. a key declared twice inside the same section: the object literal keeps
 *      the last one, so an edit to the first copy silently does nothing.
 *      This is a hard failure too.
 *
 * Reported without failing: keys translated in `en` but missing in some other
 * locale (large pre-existing debt; those locales fall back to `en` at runtime).
 *
 * Locale tables are loaded as real modules (esbuild) rather than parsed, so a
 * locale that spreads another (zh-Hant over zh) is measured by its effective
 * content.
 *
 * Usage:  npm run i18n-audit [-- --locale-gaps]
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import esbuild from "esbuild";

const here = path.dirname(fileURLToPath(import.meta.url));
const desktop = path.join(here, "..");
const SRC = path.join(desktop, "renderer", "src");
const LOCALES = path.join(SRC, "locales");
const OUT = path.join(desktop, "tmp", "i18n-audit");
const showGaps = process.argv.includes("--locale-gaps");

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

const flat = {};

const localeFiles = fs
  .readdirSync(LOCALES)
  .filter((f) => f.endsWith(".ts") && f !== "index.ts" && f !== "registry.ts")
  .map((f, i) => ({ file: f, tag: `locale${i}` }));

for (const { file, tag } of localeFiles) {
  esbuild.buildSync({
    entryPoints: [path.join(LOCALES, file)],
    bundle: true,
    format: "esm",
    platform: "neutral",
    outfile: path.join(OUT, `${tag}.mjs`),
    logLevel: "silent",
  });
  flat[file.replace(".ts", "")] = null; // placeholder keeps declaration order
}

for (const { file, tag } of localeFiles) {
  const mod = await import(pathToFileURL(path.join(OUT, `${tag}.mjs`)).href);
  const keys = new Set();
  for (const obj of Object.values(mod)) {
    if (!obj || typeof obj !== "object") continue;
    (function walk(o, prefix) {
      for (const [k, v] of Object.entries(o)) {
        if (v && typeof v === "object") walk(v, `${prefix}${k}.`);
        else keys.add(`${prefix}${k}`);
      }
    })(obj, "");
  }
  flat[file.replace(".ts", "")] = keys;
}

// ---- used keys, from every non-locale source file
const tsFiles = [];
(function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) { if (p !== LOCALES) walk(p); }
    else if (e.name.endsWith(".ts")) tsFiles.push(p);
  }
})(SRC);

const used = new Map();
for (const f of tsFiles) {
  const txt = fs.readFileSync(f, "utf8");
  for (const m of txt.matchAll(/\bt\(\s*["']([A-Za-z0-9_.-]+)["']/g)) {
    const key = m[1];
    // A trailing dot means the call is a template literal built from a prefix
    // (`t(\`sessionInner.${name}\`)`), not a complete static key.
    if (!key.includes(".") || key.endsWith(".")) continue;
    if (!used.has(key)) used.set(key, new Set());
    used.get(key).add(path.relative(SRC, f));
  }
}

// ---- duplicate declarations inside one section (textual pass)
const dupProblems = [];
for (const f of localeFiles) {
  const lines = fs.readFileSync(path.join(LOCALES, f.file), "utf8").split("\n");
  const scrub = (line) => {
    let out = "", i = 0, q = null;
    while (i < line.length) {
      const c = line[i];
      if (q) { if (c === "\\") { out += "  "; i += 2; continue; } if (c === q) q = null; out += " "; i++; continue; }
      if (c === '"' || c === "'" || c === "`") { q = c; out += " "; i++; continue; }
      if (c === "/" && line[i + 1] === "/") break;
      out += c; i++;
    }
    return out;
  };
  const stack = [];
  for (let i = 0; i < lines.length; i++) {
    const sec = lines[i].match(/^  ([A-Za-z0-9_]+): \{/);
    if (sec) { stack.push({ name: sec[1], keys: new Map(), depth: 1 }); continue; }
    if (!stack.length) continue;
    const top = stack[stack.length - 1];
    const key = (lines[i].match(/^ {4}([A-Za-z0-9_]+):/) || [])[1];
    if (key) {
      if (top.keys.has(key)) dupProblems.push(`${f.file}: duplicate "${key}" inside section "${top.name}" (lines ${top.keys.get(key)}, ${i + 1})`);
      else top.keys.set(key, i + 1);
    }
    let delta = 0;
    for (const ch of scrub(lines[i])) { if (ch === "{") delta++; else if (ch === "}") delta--; }
    top.depth += delta;
    if (top.depth <= 0) stack.pop();
  }
}

const en = flat.en || new Set();
const unresolved = [...used.keys()].filter((k) => !en.has(k)).sort();
const gapCounts = {};
for (const [loc, keys] of Object.entries(flat)) {
  if (loc === "en" || !keys) continue;
  gapCounts[loc] = [...used.keys()].filter((k) => en.has(k) && !keys.has(k)).length;
}

console.log(`locales: ${Object.keys(flat).length}  |  en keys: ${en.size}  |  keys used by the renderer: ${used.size}`);
console.log(`\nUNRESOLVED (renders the raw key on screen) -- ${unresolved.length}`);
for (const k of unresolved) console.log(`  ${k}  <- ${[...used.get(k)].join(", ")}`);
console.log(`\nDUPLICATE DECLARATIONS (last one silently wins) -- ${dupProblems.length}`);
for (const d of dupProblems.slice(0, 20)) console.log(`  ${d}`);
if (dupProblems.length > 20) console.log(`  ... ${dupProblems.length - 20} more`);
console.log(`\nper-locale coverage gaps vs en (runtime falls back to en):`);
for (const [loc, n] of Object.entries(gapCounts).sort((a, b) => b[1] - a[1])) console.log(`  ${loc.padEnd(9)} ${n}`);
if (showGaps) {
  for (const [loc, keys] of Object.entries(flat)) {
    if (loc === "en" || !keys) continue;
    const miss = [...used.keys()].filter((k) => en.has(k) && !keys.has(k));
    if (miss.length) console.log(`\n[${loc}] missing:\n  ${miss.join("\n  ")}`);
  }
}

fs.rmSync(OUT, { recursive: true, force: true });
process.exit(unresolved.length || dupProblems.length ? 1 : 0);
