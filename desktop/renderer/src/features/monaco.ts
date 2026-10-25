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

let monacoReady: Promise<typeof monaco.editor> | null = null;
let monacoEditor: typeof monaco.editor | null = null;
let monacoReject: ((err: Error) => void) | null = null;
let loaderError: Error | null = null;
const editors = new Set<any>();
let themesDefined = false;

function defineEncreThemes() {
  if (themesDefined || !monacoEditor) return;
  themesDefined = true;

  const selectionColors = {
    "editor.selectionBackground": "rgba(150, 240, 200, 0.35)",
    "editor.inactiveSelectionBackground": "rgba(0, 184, 148, 0.12)",
    "editor.selectionHighlightBackground": "rgba(0, 184, 148, 0.15)",
  };

  // Scrollbar thumbs sized to the app-wide 4px bar (see editorViewOptions):
  // subtle --border-like at rest, --text-muted-strength on hover/active.
  const darkColors = {
    ...selectionColors,
    "scrollbar.shadow": "rgba(0, 0, 0, 0)",
    "scrollbarSlider.background": "rgba(128, 128, 128, 0.28)",
    "scrollbarSlider.hoverBackground": "rgba(160, 160, 160, 0.45)",
    "scrollbarSlider.activeBackground": "rgba(160, 160, 160, 0.60)",
  };
  const lightColors = {
    ...selectionColors,
    "scrollbar.shadow": "rgba(0, 0, 0, 0)",
    "scrollbarSlider.background": "rgba(100, 100, 100, 0.25)",
    "scrollbarSlider.hoverBackground": "rgba(100, 100, 100, 0.45)",
    "scrollbarSlider.activeBackground": "rgba(100, 100, 100, 0.60)",
  };

  monacoEditor.defineTheme("encre-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [],
    colors: darkColors,
  });

  monacoEditor.defineTheme("encre-light", {
    base: "vs",
    inherit: true,
    rules: [],
    colors: lightColors,
  });
}

// True once the Monaco AMD loader script has been injected into the page.
let loaderInjected = false;

/**
 * Injects the Monaco AMD loader script (monaco/vs/loader.js) and kicks off
 * the download of the full editor bundle. Previously index.html loaded
 * editor.main synchronously at startup (~3MB of JS parsed before first
 * paint); now it only starts when the first code file is opened.
 */
function ensureMonacoLoader(): void {
  const win = window as any;
  if (loaderInjected || (win.require && typeof win.require.config === "function")) return;
  loaderInjected = true;
  const script = document.createElement("script");
  script.src = "monaco/vs/loader.js";
  script.onload = () => {
    if (win.require && typeof win.require.config === "function") {
      win.require.config({ paths: { vs: "monaco/vs" } });
      win.require(["vs/editor/editor.main"], () => {});
    }
  };
  // A silent loader failure used to leave the polling loop below spinning
  // forever and the code tab a blank rectangle; surface it instead.
  script.onerror = () => {
    loaderError = new Error("Failed to load Monaco loader (monaco/vs/loader.js)");
    console.error("[monaco]", loaderError.message);
    monacoReject?.(loaderError);
  };
  document.head.appendChild(script);
}

export function getMonaco(): Promise<typeof monaco.editor> {
  if (loaderError) return Promise.reject(loaderError);
  if (monacoReady) return monacoReady;
  monacoReady = new Promise((resolve, reject) => {
    monacoReject = reject;
    const win = window as any;
    let waitedMs = 0;
    if (win.monaco && win.monaco.editor) {
      monacoEditor = win.monaco.editor;
      defineEncreThemes();
      resolve(monacoEditor);
      return;
    }
    ensureMonacoLoader();
    const check = () => {
      if (win.monaco && win.monaco.editor) {
        monacoEditor = win.monaco.editor;
        defineEncreThemes();
        resolve(monacoEditor);
      } else if (waitedMs >= 15000) {
        // The editor bundle was requested but never materialised: give up
        // rather than polling forever behind a blank panel.
        const err = new Error("Timed out waiting for the Monaco editor bundle");
        console.error("[monaco]", err.message);
        monacoReject?.(err);
      } else {
        waitedMs += 50;
        setTimeout(check, 50);
      }
    };
    check();
  });
  return monacoReady;
}

export function registerEditor(editor: any) {
  editors.add(editor);
}

export function unregisterEditor(editor: any) {
  editors.delete(editor);
}

function updateTheme() {
  const isDark = document.documentElement.getAttribute("data-theme") !== "light";
  for (const ed of editors) {
    ed.updateOptions({ theme: isDark ? "encre-dark" : "encre-light" });
  }
}

const observer = new MutationObserver(() => updateTheme());
observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

const langMap: Record<string, string> = {
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  py: "python", pyw: "python", ipynb: "python",
  rs: "rust", go: "go", mod: "go",
  java: "java", class: "java",
  c: "c", cpp: "cpp", cxx: "cpp", h: "c", hpp: "cpp", cs: "csharp",
  swift: "swift", kt: "kotlin", scala: "scala",
  php: "php", rb: "ruby", rbs: "ruby",
  dart: "dart", lua: "lua", r: "r", rmd: "r",
  html: "html", htm: "html", css: "css", scss: "scss", sass: "scss", less: "less",
  json: "json", jsonc: "json", xml: "xml", svg: "xml",
  yaml: "yaml", yml: "yaml", toml: "yaml",
  md: "markdown", mdx: "markdown", txt: "plaintext", rtf: "plaintext",
  pdf: "plaintext",
  sh: "shell", bash: "shell", zsh: "shell", fish: "shell", ps1: "powershell", bat: "bat",
  sql: "sql", sqlite: "sql", db: "sql",
  dockerfile: "dockerfile", dockerignore: "plaintext",
  env: "plaintext", editorconfig: "ini",
  gitignore: "plaintext", gitattributes: "ini",
  png: "plaintext", jpg: "plaintext", jpeg: "plaintext",
  gif: "plaintext", webp: "plaintext", ico: "plaintext",
  zip: "plaintext", tar: "plaintext", gz: "plaintext", rar: "plaintext",
  mp3: "plaintext", mp4: "plaintext", wav: "plaintext",
  csv: "plaintext", xlsx: "plaintext", xls: "plaintext",
};

export function monacoLang(ext: string): string {
  return langMap[ext] || "plaintext";
}