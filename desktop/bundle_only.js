// Bundle-only build: main/preload/renderer esbuild steps from build.js,
// skipping the native-extension sync (which EBUSYs while the app runs).
const path = require("path");
const fs = require("fs");
const esbuild = require("esbuild");
const desktop = __dirname;
const buildLocales = ["zh", "en", "zh-Hant", "ja", "ko", "de", "es", "pt", "tr", "ar", "he"];
esbuild.buildSync({ entryPoints: [path.join(desktop, "main.ts")], bundle: true, platform: "node", target: "node20", outfile: path.join(desktop, "dist", "main.js"), external: ["electron", "node-pty", "ws"] });
console.log("main.js bundled");
esbuild.buildSync({ entryPoints: [path.join(desktop, "preload.ts")], bundle: true, platform: "node", target: "node20", outfile: path.join(desktop, "dist", "preload.js"), external: ["electron"] });
console.log("preload.js bundled");
const chunksDir = path.join(desktop, "renderer", "chunks");
if (fs.existsSync(chunksDir)) fs.rmSync(chunksDir, { recursive: true, force: true });
esbuild.buildSync({ entryPoints: [path.join(desktop, "renderer", "src", "app.ts")], bundle: true, platform: "browser", target: "es2022", outdir: path.join(desktop, "renderer"), entryNames: "bundle", chunkNames: "chunks/[name]-[hash]", jsx: "automatic", jsxImportSource: "react", format: "esm", splitting: true, minify: true, legalComments: "inline", define: { BUILD_LOCALES_LIST: JSON.stringify(buildLocales) } });
console.log("renderer bundle rebuilt");
