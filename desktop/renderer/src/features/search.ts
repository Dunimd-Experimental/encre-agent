/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
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
 * Global command palette / search.
 *
 * Implements the ⌘K-style search overlay. It merges server-side search results
 * with locally-indexed entities (skills, workspaces, models, settings nav, app
 * actions, slash commands, memory, rules, sessions, tools, automation, workflow,
 * and — for every *registered* workspace — its files) via MiniSearch — a
 * production-grade, zero-dependency, offline full-text search engine — using the
 * browser/Electron built-in `Intl.Segmenter` for CJK (Chinese) word segmentation,
 * so Chinese queries actually match. Results are grouped into labeled sections
 * and the appropriate action is dispatched when a result is activated.
 *
 * Both the ⌘K palette and the in-settings sidebar search share the same match
 * logic (`runLocalSearch` / `searchSettingsNavItems`) and the same user
 * configurable search filter (`AppState.searchFilter`).
 */

import MiniSearch from "minisearch";
import { getState, subscribe, setSearchResults, addAttachments, getAppliedSearchSeq } from "../core/state.js";
import { send } from "../core/ws.js";
import { setRequestedSessionId } from "../core/stream.js";
import { guardRender } from "../core/ui_boundary.js";
import { scheduleRender } from "../core/render_scheduler.js";
import type { SearchResultEntry, SearchFilter, SearchFilterKey, PluginUiResult } from "../core/types.js";
import { t, onLocaleChange } from "./i18n.js";
import { pluginSearchModes, sendPluginUiAction, onPluginUiResult, CONTRIBUTIONS_CHANGED } from "./plugin_contributions.js";
import { matchingSlashCommands, SLASH_COMMANDS } from "../ui/slash-commands.js";
import { getFileIcon } from "./files.js";
import { hideAnimated, showAnimated } from "../ui/animate.js";

/**
 * Registry of app-action callbacks invokable from search results.
 * Populated by `app.ts` (registerCommandActions); search reads it at runtime
 * so any newly registered action becomes searchable automatically.
 */
export const commandActions: Record<string, () => void> = {};

/** A locally-indexed item fed into the MiniSearch full-text index. */
interface LocalItem {
  id?: number;
  kind: string;
  name: string;
  snippet: string;
  preview?: string;
  path?: string;
  icon?: string;
  session_id?: string;
  workspace_id?: string;
}

/** Convenience alias for the reactive app-state shape. */
type AppStateT = ReturnType<typeof getState>;

/** Template describing how a search section is labeled/iconed. */
interface SectionTemplate {
  getLabel: () => string;
  icon: string;
  kindLabel: (r: SearchResultEntry) => string;
}

/** A concrete search section: template plus its matched items. */
interface SectionDef extends SectionTemplate {
  items: SearchResultEntry[];
}

const SECTION_DEFS: Record<string, SectionTemplate> = {
  session_header: {
    getLabel: () => t("search.sectionSessions"),
    icon: `<i data-lucide="message-square" class="lucide lucide-sm"></i>`,
    // Show the session title (user name or first-message preview), never the id.
    kindLabel: (r) => r.name || r.preview || t("search.resultSession"),
  },
  memory: {
    getLabel: () => t("search.sectionMemory"),
    icon: `<i data-lucide="brain" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.path || t("search.resultMemory"),
  },
  global_rule: {
    getLabel: () => t("search.sectionGlobalRules"),
    icon: `<i data-lucide="file-check" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.path || t("search.resultGlobalRule"),
  },
  project_rule: {
    getLabel: () => t("search.sectionProjectRules"),
    icon: `<i data-lucide="file-check" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.path || t("search.resultProjectRule"),
  },
  skill: {
    getLabel: () => t("search.sectionSkills"),
    icon: `<i data-lucide="wand-2" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.name || t("search.resultSkill"),
  },
  workspace: {
    getLabel: () => t("search.sectionWorkspaces"),
    icon: `<i data-lucide="folder" class="lucide lucide-sm"></i>`,
    kindLabel: () => t("search.resultWorkspace"),
  },
  model: {
    getLabel: () => t("search.sectionModels"),
    icon: `<i data-lucide="cpu" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.name || t("search.resultModel"),
  },
  mcp_server: {
    getLabel: () => t("search.sectionMcpServers"),
    icon: `<i data-lucide="server" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.name || t("search.resultMcpServer"),
  },
  sub_agent: {
    getLabel: () => t("search.sectionSubAgents"),
    icon: `<i data-lucide="sparkles" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.name || t("search.resultSubAgent"),
  },
  document: {
    getLabel: () => t("search.sectionDocuments"),
    icon: `<i data-lucide="book" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.name || t("search.resultDocument"),
  },
  notification: {
    getLabel: () => t("search.sectionNotifications"),
    icon: `<i data-lucide="bell" class="lucide lucide-sm"></i>`,
    kindLabel: () => t("search.resultNotification"),
  },
  custom_command: {
    getLabel: () => t("search.sectionCommands"),
    icon: `<i data-lucide="terminal" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => `/${r.name}` || t("search.resultCommand"),
  },
  settings_nav: {
    getLabel: () => t("search.sectionSettings"),
    icon: `<i data-lucide="settings" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.preview || "",
  },
  app_action: {
    getLabel: () => t("search.sectionActions"),
    icon: `<i data-lucide="zap" class="lucide lucide-sm"></i>`,
    kindLabel: (r) => r.preview || "",
  },
  slash_command: {
    getLabel: () => t("search.sectionSlashCommands"),
    icon: `<i data-lucide="wand-2" class="lucide lucide-sm"></i>`,
    kindLabel: () => t("search.resultSlashCommand"),
  },
  tool: {
    getLabel: () => t("search.sectionTools"),
    icon: `<i data-lucide="layout-grid" class="lucide lucide-sm"></i>`,
    kindLabel: () => t("search.resultTool"),
  },
  automation: {
    getLabel: () => t("search.sectionAutomation"),
    icon: `<i data-lucide="list-checks" class="lucide lucide-sm"></i>`,
    kindLabel: () => t("search.resultAutomation"),
  },
  workflow: {
    getLabel: () => t("search.sectionWorkflow"),
    icon: `<i data-lucide="git-branch" class="lucide lucide-sm"></i>`,
    kindLabel: () => t("search.resultWorkflow"),
  },
  file: {
    getLabel: () => t("search.sectionFiles"),
    icon: `<i data-lucide="file" class="lucide lucide-sm"></i>`,
    // Only the workspace name here — the file path lives in `snippet` so it
    // keeps its original case (kind label styling must not uppercase it).
    kindLabel: (r) => r.preview || r.kind,
  },
};

/** Every settings panel the app can navigate to (must match `PanelId`). */
export const SETTINGS_NAV_ITEMS: { panel: string; nameEn: string; nameZh: string }[] = [
  { panel: "general", nameEn: "General", nameZh: "常规" },
  { panel: "model", nameEn: "Model", nameZh: "模型管理" },
  { panel: "mcp", nameEn: "MCP", nameZh: "MCP 服务器" },
  { panel: "skills", nameEn: "Skills & Commands", nameZh: "技能与命令" },
  { panel: "flow", nameEn: "Flow", nameZh: "对话流" },
  { panel: "plugins", nameEn: "Plugins", nameZh: "插件" },
  { panel: "agent", nameEn: "Sub-Agents", nameZh: "子智能体管理" },
  { panel: "library", nameEn: "Library", nameZh: "资料库" },
  { panel: "memory", nameEn: "Memory", nameZh: "记忆" },
  { panel: "usage", nameEn: "Usage", nameZh: "使用统计" },
  { panel: "about", nameEn: "About", nameZh: "关于" },
  { panel: "shortcuts", nameEn: "Shortcuts", nameZh: "快捷键" },
  { panel: "storage", nameEn: "Storage", nameZh: "存储" },
  { panel: "assist", nameEn: "Assist Panels", nameZh: "辅助面板" },
  { panel: "gateway", nameEn: "Connector", nameZh: "连接器" },
  { panel: "developer", nameEn: "Developer", nameZh: "开发者" },
];

/** i18n display names for registered command actions. */
interface AppActionLabel {
  en: string;
  zh: string;
  /** Extra search keywords (e.g. common colloquial phrasings). */
  alias?: string[];
}
const APP_ACTION_LABELS: Record<string, AppActionLabel> = {
  "settings": { en: "Settings", zh: "设置", alias: ["打开设置", "进入设置", "偏好设置"] },
  "model-management": { en: "Model Management", zh: "模型管理", alias: ["配置模型", "模型设置"] },
  "skills-management": { en: "Skills & Commands", zh: "技能与命令", alias: ["配置技能", "技能设置", "规则"] },
  "mcp-servers": { en: "MCP Servers", zh: "MCP 服务器", alias: ["MCP 配置", "配置 MCP"] },
  "agent-config": { en: "Sub-Agent Config", zh: "子智能体配置", alias: ["配置子智能体", "子智能体", "子代理"] },
  "about": { en: "About", zh: "关于", alias: ["关于应用", "版本信息"] },
  "theme-dark": { en: "Dark Theme", zh: "深色主题", alias: ["暗色主题", "夜间模式"] },
  "theme-light": { en: "Light Theme", zh: "浅色主题", alias: ["亮色主题", "日间模式"] },
  "theme-system": { en: "System Theme", zh: "跟随系统主题", alias: ["系统主题", "自动主题"] },
  "language-zh": { en: "Language: Chinese", zh: "语言：中文", alias: ["中文", "简体中文"] },
  "language-en": { en: "Language: English", zh: "语言：英文", alias: ["英文", "英语"] },
  "language-zh-Hant": { en: "繁體中文", zh: "繁體中文", alias: ["繁体", "繁體", "正體中文", "Traditional"] },
  "language-ja": { en: "日本語", zh: "日本語", alias: ["日语", "日文", "Japanese"] },
  "language-ko": { en: "한국어", zh: "한국어", alias: ["韩语", "韓語", "Korean"] },
  "language-de": { en: "Deutsch", zh: "Deutsch", alias: ["德语", "德文", "German"] },
  "language-tr": { en: "Türkçe", zh: "Türkçe", alias: ["土耳其语", "Turkish"] },
  "language-es": { en: "Español", zh: "Español", alias: ["西班牙语", "Spanish"] },
  "language-pt": { en: "Português", zh: "Português", alias: ["葡萄牙语", "Portuguese"] },
  "language-ar": { en: "العربية", zh: "العربية", alias: ["阿拉伯语", "Arabic"] },
  "language-he": { en: "עברית", zh: "עברית", alias: ["希伯来语", "Hebrew"] },
  "keyboard-shortcuts": { en: "Keyboard Shortcuts", zh: "键盘快捷键", alias: ["快捷键", "快捷键设置", "按键"] },
  "terminal-settings": { en: "Terminal Panel", zh: "终端面板", alias: ["终端", "命令行", "shell", "terminal"] },
  "editor-settings": { en: "Editor Panel", zh: "编辑器面板", alias: ["编辑器", "编辑设置", "editor"] },
  "review-settings": { en: "Review Panel", zh: "审查面板", alias: ["审查", "代码审查", "diff", "review"] },
  "browser-settings": { en: "Browser Panel", zh: "浏览器面板", alias: ["浏览器", "内置浏览器", "browser"] },
};

/** Action ids exposed as dedicated search categories (not under "Actions"). */
const DEDICATED_ACTION_KINDS = new Set(["automation", "workflow"]);

// ── Mature full-text search engine (MiniSearch) + built-in CJK tokenizer ─────
// Replaces the previous Fuse.js fuzzy matcher, which is unreliable for Chinese
// (CJK) queries. MiniSearch is a production-grade, zero-dependency, offline
// in-memory search engine; Chinese word segmentation is provided by the
// browser/Electron built-in `Intl.Segmenter` (TC39 standard, no npm dep).
// Because both the index and the query use the same segmenter, Chinese
// substring and prefix queries match correctly.

const SegmenterCtor: any =
  typeof Intl !== "undefined" && (Intl as any).Segmenter ? (Intl as any).Segmenter : null;
const cjkSegmenter: any = SegmenterCtor
  ? new SegmenterCtor("zh", { granularity: "word" })
  : null;

/** Tokenizer: word-segments CJK and keeps latin/number runs; lower-cased. */
function cjkTokenize(text: string): string[] {
  if (!text) return [];
  if (!cjkSegmenter) {
    // Fallback for environments without Intl.Segmenter.
    return text.toLowerCase().split(/[^一-龥a-z0-9]+/i).filter(Boolean);
  }
  const out: string[] = [];
  for (const seg of cjkSegmenter.segment(text)) {
    if (seg.isWordLike) {
      const tok = (seg.segment as string).toLowerCase();
      if (tok) out.push(tok);
    }
  }
  return out;
}

let _searchIndex: MiniSearch<LocalItem> | null = null;
let _indexSignature = "";

/** Drop the cached local-search index (call when underlying data changes). */
export function invalidateSearchIndex(): void {
  _searchIndex = null;
}

/** Cheap signature of all data sources that feed the search index. */
function dataSourceSignature(st: AppStateT): string {
  const ti = st.toolsInfo;
  return [
    st.skillsList.length,
    st.workspaces.length,
    st.modelConfigs.length,
    st.mcpServers.length,
    st.subAgents.length,
    st.libraryTree.length,
    st.notifications.length,
    st.customCommands.length,
    st.memoryList.length,
    st.globalRules.length,
    st.projectRules.length,
    st.sessionsList.length,
    (ti?.base?.length || 0) + (ti?.unlocked?.length || 0) + (ti?.active?.length || 0),
    st.automationHistory.length,
    st.workflowState ? 1 : 0,
    cachedWorkspaceFiles.length,
    Object.keys(commandActions).length,
    st.workspaceMode,
  ].join(",");
}

/** Returns a cached MiniSearch index, rebuilding only when data changed. */
function getIndex(items: LocalItem[], st: AppStateT): MiniSearch<LocalItem> {
  const sig = dataSourceSignature(st);
  if (_searchIndex && _indexSignature === sig) return _searchIndex;
  const mini = new MiniSearch<LocalItem>({
    idField: "id",
    fields: ["name", "snippet", "preview", "path", "session_id", "icon"],
    storeFields: ["kind", "name", "snippet", "preview", "path", "session_id", "icon", "workspace_id"],
    tokenize: cjkTokenize,
    processTerm: (term: string) => term.toLowerCase(),
  });
  for (let i = 0; i < items.length; i++) items[i].id = i;
  mini.addAll(items);
  _searchIndex = mini;
  _indexSignature = sig;
  return mini;
}

/** Builds the lower-cased haystack string for an item (substring recall). */
function haystackOf(it: LocalItem): string {
  return [it.name, it.snippet, it.preview, it.path, it.session_id, it.icon]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

/** Maps a LocalItem to the public SearchResultEntry shape. */
function toEntry(it: LocalItem): SearchResultEntry {
  return {
    kind: it.kind,
    name: it.name,
    snippet: it.snippet,
    preview: it.preview,
    path: it.path,
    session_id: it.session_id,
    icon: it.icon,
    workspace_id: it.workspace_id,
  } as SearchResultEntry;
}

/** User-facing labels for each search-filter toggle, shared by the settings UI. */
export const SEARCH_FILTER_META: { key: SearchFilterKey; zh: string; en: string }[] = [
  { key: "session", zh: "会话", en: "Sessions" },
  { key: "memory", zh: "记忆", en: "Memory" },
  { key: "global_rule", zh: "全局规则", en: "Global Rules" },
  { key: "project_rule", zh: "项目规则", en: "Project Rules" },
  { key: "skill", zh: "技能", en: "Skills" },
  { key: "workspace", zh: "工作区", en: "Workspaces" },
  { key: "workspace_file", zh: "工作区文件", en: "Workspace Files" },
  { key: "model", zh: "模型", en: "Models" },
  { key: "mcp_server", zh: "MCP 服务", en: "MCP Servers" },
  { key: "sub_agent", zh: "子代理", en: "Sub Agents" },
  { key: "document", zh: "资料库", en: "Library" },
  { key: "notification", zh: "通知", en: "Notifications" },
  { key: "settings", zh: "设置项", en: "Settings" },
  { key: "action", zh: "操作", en: "Actions" },
  { key: "slash_command", zh: "斜杠命令", en: "Slash Commands" },
  { key: "custom_command", zh: "命令", en: "Commands" },
  { key: "tool", zh: "工具", en: "Tools" },
  { key: "automation", zh: "自动化", en: "Automation" },
  { key: "workflow", zh: "工作流", en: "Workflow" },
];

/** Returns true when `name` is a known Lucide icon (safe for data-lucide). */
function isKnownLucideIcon(name: string): boolean {
  const n = name.trim();
  if (!n) return false;
  const lucide = (window as any).lucide;
  if (!lucide || !lucide.icons) return true;
  const pascal = n.split("-").map((p) => (p ? p[0].toUpperCase() + p.slice(1) : p)).join("");
  return n in lucide.icons || pascal in lucide.icons;
}

const SECTION_ORDER = [
  "session_header", "memory", "global_rule", "project_rule",
  "app_action", "settings_nav", "slash_command",
  "skill", "workspace", "model", "mcp_server", "sub_agent", "document",
  "notification", "custom_command", "tool", "automation", "workflow", "file",
];

/**
 * Maps local result kinds to the user-facing search-filter key they belong to.
 * Most kinds share their key with the filter (e.g. `skill` -> `skill`), but
 * several don't: `session_header` lives under the `session` toggle,
 * workspace `file` entries under `workspace_file`, `settings_nav` under
 * `settings`, and `app_action` under `action`. Without this mapping the
 * `filter[kind]` lookup yields `undefined` (falsy) and those results are
 * silently dropped even when the toggle is enabled.
 */
const KIND_TO_FILTER_KEY: Record<string, SearchFilterKey> = {
  session_header: "session",
  conversation: "session",
  file: "workspace_file",
  settings_nav: "settings",
  app_action: "action",
};

/**
 * App-action ids that are pure navigation shortcuts opening the same settings
 * panel as a `settings_nav` entry (value = the panel id). Both appear in the
 * local index independently, so without cross-kind dedup the palette shows the
 * same settings destination twice (e.g. "模型管理" under both 设置项 and 操作).
 */
const ACTION_TO_NAV_PANEL: Record<string, string> = {
  settings: "general",
  "model-management": "model",
  "skills-management": "skills",
  "mcp-servers": "mcp",
  "agent-config": "agent",
  about: "about",
  "keyboard-shortcuts": "shortcuts",
  "terminal-settings": "assist",
  "editor-settings": "assist",
  "review-settings": "assist",
  "browser-settings": "assist",
};

/** Resolves the search-filter key for a result kind (defaults to the kind). */
function filterKeyOf(kind: string): SearchFilterKey {
  return KIND_TO_FILTER_KEY[kind] ?? (kind as SearchFilterKey);
}

// ── Workspace file index (frontend only, registered workspaces only) ─────────

/** Cache of file entries collected from every *registered* workspace. */
let cachedWorkspaceFiles: LocalItem[] = [];

const WS_FILE_EXCLUDED = new Set([
  ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "target",
  ".encre", ".pytest_cache", ".mypy_cache", "build", "out", ".idea", ".vscode",
]);

const MAX_WS_FILE_DEPTH = 5;
const MAX_WS_FILES_PER_WS = 400;

function norm(p: string): string {
  return p.replace(/\\/g, "/");
}

async function walkWorkspace(
  dir: string,
  wsName: string,
  wsPath: string,
  out: LocalItem[],
  depth: number,
  budget: number,
): Promise<void> {
  // Defensive: never walk outside the workspace root (Windows: case-insensitive).
  // Guards against junction/symlink quirks and malformed paths so the file
  // index can never leak files from outside the registered workspace.
  const root = norm(wsPath).replace(/\/+$/, "");
  const cur = norm(dir).replace(/\/+$/, "");
  if (cur !== root && !cur.toLowerCase().startsWith(`${root.toLowerCase()}/`)) return;
  if (depth > MAX_WS_FILE_DEPTH || out.length >= budget) return;
  const api = window.electronAPI;
  if (!api) return;
  let entries: { name: string; isDirectory: boolean; isFile: boolean }[] = [];
  try {
    entries = await api.listDirectory(dir);
  } catch {
    return;
  }
  for (const e of entries) {
    if (out.length >= budget) return;
    if (e.name.startsWith(".")) continue;
    const abs = norm(`${dir}/${e.name}`);
    if (e.isDirectory) {
      if (WS_FILE_EXCLUDED.has(e.name)) continue;
      await walkWorkspace(abs, wsName, wsPath, out, depth + 1, budget);
    } else if (e.isFile) {
      const baseParts = norm(wsPath).split("/");
      const absParts = abs.split("/");
      const rel = absParts.slice(baseParts.length).join("/");
      out.push({
        kind: "file",
        name: e.name,
        snippet: rel,
        preview: wsName,
        path: abs,
        workspace_id: wsPath,
      });
    }
  }
}

// Generation guard for buildWorkspaceFileIndex: it walks workspaces through
// several awaited IPC round-trips, and concurrent builds (app open, workspace
// register/unregister) could let a SLOW walk based on an older workspace set
// finish last and overwrite the fresh index with stale paths (including files
// of a workspace the user just removed).  Only the latest build may publish.
let _wsIndexGen = 0;

/** Rebuilds `cachedWorkspaceFiles` from all registered workspaces. */
export async function buildWorkspaceFileIndex(): Promise<void> {
  const gen = ++_wsIndexGen;
  const out: LocalItem[] = [];
  // `MAX_WS_FILES_PER_WS` is a *per-workspace* budget; the global cap is
  // `MAX_WS_FILES_PER_WS * 4`. Passing a per-workspace budget to the walker
  // ensures every registered workspace contributes files instead of letting
  // the first workspace (sorted arbitrarily) consume the whole index.
  const maxTotal = MAX_WS_FILES_PER_WS * 4;
  for (const ws of getState().workspaces) {
    if (out.length >= maxTotal) break;
    const before = out.length;
    try {
      await walkWorkspace(norm(ws.path), ws.name, norm(ws.path), out, 0, before + MAX_WS_FILES_PER_WS);
    } catch {
      /* ignore unreadable workspace */
    }
    // A newer build started while we were walking: drop this stale one.
    if (gen !== _wsIndexGen) return;
  }
  if (gen !== _wsIndexGen) return;
  cachedWorkspaceFiles = out;
  invalidateSearchIndex();
}

/**
 * Core local search used by both the ⌘K palette and the settings sidebar.
 * Returns merged, de-duplicated results honoring the user's search filter.
 */
export function runLocalSearch(q: string, filter: SearchFilter): SearchResultEntry[] {
  const query = q.trim();
  if (!query) return [];
  const st = getState();
  const ql = query.toLowerCase();
  const items: LocalItem[] = [];

  // Build the FULL item set (filtering is applied to results, not the index,
  // so the cached MiniSearch index stays stable across filter toggles).
  for (const nav of SETTINGS_NAV_ITEMS) {
    items.push({ kind: "settings_nav", name: nav.nameEn, snippet: nav.nameZh, preview: nav.panel });
  }

  for (const id of Object.keys(commandActions)) {
    if (DEDICATED_ACTION_KINDS.has(id)) continue;
    const label = APP_ACTION_LABELS[id] || { en: id, zh: id };
    const aliasText = label.alias ? ` ${label.alias.join(" ")}` : "";
    items.push({ kind: "app_action", name: label.en, snippet: `${label.zh}${aliasText}`, preview: id, path: id });
  }

  for (const sk of st.skillsList) items.push({ kind: "skill", name: sk.name, snippet: sk.name, preview: sk.description });
  for (const ws of st.workspaces) items.push({ kind: "workspace", name: ws.name, snippet: ws.name, preview: ws.path, path: ws.path });
  for (const mc of st.modelConfigs) items.push({ kind: "model", name: mc.name, snippet: mc.name, preview: mc.model_id });
  for (const sv of st.mcpServers) items.push({ kind: "mcp_server", name: sv.name, snippet: sv.name });
  for (const sa of st.subAgents) items.push({ kind: "sub_agent", name: sa.name, snippet: sa.name, preview: sa.description });
  for (const node of st.libraryTree) {
    if (node.type === "space" || node.type === "folder") continue;
    items.push({ kind: "document", name: node.name, snippet: node.name });
  }
  for (const n of st.notifications) items.push({ kind: "notification", name: n.title, snippet: n.title, preview: n.message });
  for (const cc of st.customCommands) items.push({ kind: "custom_command", name: cc.name, snippet: cc.title, preview: cc.description });
  for (const m of st.memoryList) items.push({ kind: "memory", name: m.name, snippet: m.path, preview: m.preview, path: m.path });
  for (const r of st.globalRules) items.push({ kind: "global_rule", name: r.name, snippet: r.path, preview: r.path, path: r.path });
  for (const r of st.projectRules) items.push({ kind: "project_rule", name: r.name, snippet: r.path, preview: `${r.priority}`, path: r.path });

  // Session title/ID search uses the global session cache (kept across mode
  // switches), falling back to the mode-scoped sidebar list.
  const sessionPool = st.allSessions.length > 0 ? st.allSessions : st.sessionsList;
  for (const s of sessionPool) {
    const title = s.name || s.preview || s.session_id;
    items.push({
      kind: "session_header",
      name: title,
      snippet: title,
      preview: s.preview,
      session_id: s.session_id,
    });
  }

  const ti = st.toolsInfo;
  const seenTools = new Set<string>();
  for (const list of [ti.base, ti.unlocked, ti.active]) {
    for (const tool of list) {
      if (seenTools.has(tool)) continue;
      seenTools.add(tool);
      items.push({ kind: "tool", name: tool, snippet: tool, preview: t("search.resultTool") });
    }
  }

  for (const h of st.automationHistory) {
    const name = h.name || h.job_id || h.id || "automation";
    items.push({ kind: "automation", name, snippet: `${h.state || ""} ${h.name || ""}`.trim(), preview: h.job_id, path: h.id });
  }

  if (st.workflowState) {
    const wf = st.workflowState;
    items.push({
      kind: "workflow",
      name: wf.goal,
      snippet: wf.goal,
      preview: `${wf.completedCount}/${wf.totalTasks}`,
      path: wf.workflowId,
    });
  }

  for (const f of cachedWorkspaceFiles) items.push(f);

  const cmds = matchingSlashCommands(query, st.workspaceMode);
  for (const cmd of cmds) {
    items.push({ kind: "slash_command", name: `/${cmd.name}`, snippet: cmd.title, preview: cmd.description, icon: cmd.icon });
  }

  // Assign stable positional ids to *every* item, regardless of whether
  // `getIndex` hits its cache. Items are built in a fixed order, so on a
  // cache hit the ids match the index exactly; without this the recall pass
  // below would see all-undefined ids and dedupe every result into one.
  for (let i = 0; i < items.length; i++) items[i].id = i;

  const mini = getIndex(items, st);

  // Primary pass: MiniSearch ranked results (CJK-aware via Intl.Segmenter).
  const ranked: SearchResultEntry[] = [];
  const seenId = new Set<number>();
  try {
    const hits = mini.search(query, {
      prefix: true,
      fuzzy: 0.2,
      combineWith: "AND",
      boost: { name: 2, snippet: 1.5 },
    });
    for (const h of hits) {
      const id = h.id as number;
      if (seenId.has(id)) continue;
      const item = h as unknown as LocalItem;
      if (!filter[filterKeyOf(item.kind)]) continue;
      seenId.add(id);
      ranked.push(toEntry(item));
    }
  } catch {
    /* ignore search errors */
  }

  // Recall fallback: case-insensitive substring. Guarantees CJK substring
  // matches (including a single mid-string character) that prefix/fuzzy may
  // miss. MiniSearch remains the primary ranked engine; this is just recall.
  for (const it of items) {
    if (seenId.has(it.id as number)) continue;
    if (!filter[filterKeyOf(it.kind)]) continue;
    if (haystackOf(it).includes(ql)) {
      seenId.add(it.id as number);
      ranked.push(toEntry(it));
    }
  }

  return ranked;
}

/**
 * Lightweight settings-nav matcher for the in-settings sidebar search.
 * Shares `SETTINGS_NAV_ITEMS` with the palette so both stay in sync.
 */
export function searchSettingsNavItems(q: string): { panel: string; nameEn: string; nameZh: string }[] {
  if (!q || !q.trim()) return SETTINGS_NAV_ITEMS;
  const lower = q.trim().toLowerCase();
  return SETTINGS_NAV_ITEMS.filter(
    (n) => n.nameEn.toLowerCase().includes(lower) || n.nameZh.toLowerCase().includes(lower),
  );
}

/**
 * Shared monotonic counter for backend search requests. Used by the
 * ⌘K palette so stale responses are dropped in `applySearchResults` by
 * seq comparison.
 */
export let searchSeqCounter = 0;

/** Returns the next shared backend-search sequence number (monotonic). */
export function nextSearchSeq(): number {
  return ++searchSeqCounter;
}

/**
 * The search command palette controller.
 */
export class Search {
  private input: HTMLInputElement;
  private resultsEl: HTMLElement;
  private selectedIdx = -1;
  private timer = 0;
  private currentResults: SearchResultEntry[] = [];
  private lastWsRef: unknown = null;
  /** True while a backend search request is in flight (shows the spinner). */
  private searching = false;
  /** Sequence of the newest backend results already applied to state. */
  private lastAppliedSeq = -1;
  /** Plugin-contributed search modes (declarative contributions). */
  private pluginModes: ReturnType<typeof pluginSearchModes> = [];
  /** Currently active plugin mode (empty = the default first-party search). */
  private activePlugin = "";
  private activeModeId = "";
  /** Results returned by the active plugin mode through `plugin_ui_result`. */
  private pluginResults: SearchResultEntry[] = [];

  /**
   * Constructor: resolves DOM elements, wires input/keys, and kicks off the
   * workspace-file index (refreshed whenever the registered workspaces change).
   */
  constructor() {
    this.input = document.getElementById("search-input") as HTMLInputElement;
    this.resultsEl = document.getElementById("search-results")!;

    this.input.addEventListener("input", () => this.onInput());
    this.input.addEventListener("keydown", (e) => this.onKey(e));
    subscribe(() => this.onStateChange(), ["workspaces"]);
    onLocaleChange(() => this.renderResults());

    this.pluginModes = pluginSearchModes();
    // Contribution set changed (plugin reload): refresh the mode chips and
    // drop an active mode that no longer exists.
    window.addEventListener(CONTRIBUTIONS_CHANGED, () => {
      this.pluginModes = pluginSearchModes();
      if (this.activeModeId && !this.pluginModes.some((m) => m.plugin === this.activePlugin && m.id === this.activeModeId)) {
        this.activePlugin = "";
        this.activeModeId = "";
        this.pluginResults = [];
      }
      this.renderResults();
    });
    // Chip clicks activate/deactivate a plugin search mode.
    this.resultsEl.addEventListener("click", (e) => {
      const chip = (e.target as HTMLElement).closest?.(".search-plugin-mode-chip") as HTMLElement | null;
      if (!chip) return;
      this.togglePluginMode(chip.dataset.plugin || "", chip.dataset.mode || "");
    });
    // Plugin modes answer through the unified `plugin_ui_result` event.
    onPluginUiResult((res) => this.onPluginResult(res));

    this.refreshWorkspaceFiles();
  }

  /** Activates (or deactivates) a plugin search mode chip. */
  private togglePluginMode(plugin: string, id: string): void {
    if (this.activePlugin === plugin && this.activeModeId === id) {
      this.activePlugin = "";
      this.activeModeId = "";
    } else {
      this.activePlugin = plugin;
      this.activeModeId = id;
    }
    this.pluginResults = [];
    this.searching = false;
    this.selectedIdx = -1;
    this.renderResults();
    const q = this.input.value.trim();
    if (q && this.activeModeId) this.dispatchPluginSearch(q);
  }

  /** Runs the active plugin mode for `q` through the unified plugin channel. */
  private dispatchPluginSearch(q: string): void {
    this.searching = true;
    this.pluginResults = [];
    this.renderResults();
    sendPluginUiAction("search_mode", this.activePlugin, this.activeModeId, { query: q });
  }

  /** Applies a `plugin_ui_result` carrying the active mode's search results. */
  private onPluginResult(res: PluginUiResult): void {
    if (res.kind !== "search_mode" || !res.ok) return;
    if (res.plugin !== this.activePlugin || res.action !== this.activeModeId) return;
    const data = (res.data && typeof res.data === "object" ? res.data : {}) as Record<string, unknown>;
    // A mode may echo the query it answered; drop stale echoes.
    if (typeof data.query === "string" && data.query !== this.input.value.trim()) return;
    const list = Array.isArray(data.results) ? data.results : [];
    const entries: SearchResultEntry[] = [];
    for (const raw of list) {
      if (!raw || typeof raw !== "object") continue;
      const o = raw as Record<string, unknown>;
      const name = typeof o.name === "string" ? o.name : "";
      const snippet = typeof o.snippet === "string" ? o.snippet : "";
      if (!name && !snippet) continue;
      entries.push({
        kind: "plugin_search",
        name,
        snippet: name || snippet,
        preview: snippet && snippet !== name ? snippet : typeof o.preview === "string" ? o.preview : undefined,
        path: typeof o.path === "string" ? o.path : undefined,
        icon: typeof o.icon === "string" ? o.icon : undefined,
        plugin: res.plugin,
      } as SearchResultEntry);
    }
    this.pluginResults = entries;
    this.searching = false;
    this.selectedIdx = -1;
    this.renderResults();
  }

  /** Opens the search overlay and clears previous state/results. */
  open(): void {
    const overlay = document.getElementById("search-overlay")!;
    showAnimated(overlay);
    this.input.value = "";
    this.selectedIdx = -1;
    this.currentResults = [];
    this.searching = false;
    this.lastAppliedSeq = searchSeqCounter;
    // The palette always opens in the default first-party mode.
    this.activePlugin = "";
    this.activeModeId = "";
    this.pluginResults = [];
    this.pluginModes = pluginSearchModes();
    setSearchResults([]);
    this.resultsEl.innerHTML = this.pluginModesHtml();
    // Rebuild the registered-workspace file index so it's current.
    this.refreshWorkspaceFiles();
    setTimeout(() => this.input.focus(), 10);
  }

  /** Closes the search overlay and clears state/results. */
  close(): void {
    hideAnimated(document.getElementById("search-overlay"));
    this.input.value = "";
    this.selectedIdx = -1;
    this.currentResults = [];
    this.searching = false;
    setSearchResults([]);
  }

  private onInput(): void {
    const q = this.input.value.trim();
    this.selectedIdx = -1;
    clearTimeout(this.timer);
    if (!q) {
      this.searching = false;
      this.lastAppliedSeq = searchSeqCounter;
      this.pluginResults = [];
      setSearchResults([]);
      this.resultsEl.innerHTML = this.pluginModesHtml();
      return;
    }
    // Treat a request as in flight as soon as the user types: stale backend
    // results from the previous query are never shown against the new query,
    // and the loading spinner appears immediately while local results render
    // synchronously (no more blank palette).
    this.searching = true;
    this.lastAppliedSeq = searchSeqCounter;
    this.renderResults();
    this.timer = window.setTimeout(() => {
      // In a plugin mode the query rides the unified plugin channel instead
      // of the first-party backend search.
      if (this.activeModeId) {
        this.dispatchPluginSearch(q);
      } else {
        send({ type: "search", query: q, seq: nextSearchSeq() });
      }
    }, 150);
  }

  private onKey(e: KeyboardEvent): void {
    if (e.key === "Escape") {
      e.preventDefault();
      this.close();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      this.selectedIdx = Math.min(this.selectedIdx + 1, this.currentResults.length - 1);
      this.renderResults();
      this.scrollToSelected();
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      this.selectedIdx = Math.max(this.selectedIdx - 1, -1);
      this.renderResults();
      this.scrollToSelected();
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (this.selectedIdx >= 0 && this.selectedIdx < this.currentResults.length) {
        this.activateResult(this.currentResults[this.selectedIdx]);
      }
      return;
    }
  }

  /** Re-indexes workspace files when the registered workspace set changes. */
  private refreshWorkspaceFiles(): void {
    buildWorkspaceFileIndex().catch((e) => console.warn("[search] workspace file index failed:", e));
  }

  private onStateChange(): void {
    const ws = getState().workspaces;
    if (ws !== this.lastWsRef) {
      this.lastWsRef = ws;
      this.refreshWorkspaceFiles();
    }
    this.renderResults();
  }

  /** Handles an activated result, dispatching by its `kind`. */
  private activateResult(r: SearchResultEntry): void {
    this.close();
    switch (r.kind) {
      case "conversation":
      case "session_header":
        if (r.session_id) {
          const requestId = crypto.randomUUID();
          setRequestedSessionId(r.session_id, requestId);
          send({ type: "resume", session_id: r.session_id, request_id: requestId });
        }
        break;
      case "file":
        if (r.path) this.activateFileResult(r.path, r.workspace_id);
        break;
      case "memory":
      case "global_rule":
      case "project_rule":
        this.insertPromptText(t("search.readEntry", { kind: r.kind, name: r.path || r.snippet }));
        break;
      case "skill":
        this.openSettingsPanel("skills");
        break;
      case "workspace":
        if (r.path) {
          const requestId = crypto.randomUUID();
          setRequestedSessionId("", requestId);
          send({ type: "open_workspace", path: r.path, request_id: requestId });
        }
        break;
      case "model":
        this.openSettingsPanel("model");
        break;
      case "mcp_server":
        this.openSettingsPanel("mcp");
        break;
      case "sub_agent":
        this.openSettingsPanel("agent");
        break;
      case "document":
        this.openSettingsPanel("library");
        break;
      case "settings_nav": {
        const panel = r.preview || "general";
        this.openSettingsPanel(panel);
        break;
      }
      case "tool":
        this.insertPromptText(r.name ?? "");
        break;
      case "automation":
        commandActions["automation"]?.();
        break;
      case "workflow":
        commandActions["workflow"]?.();
        break;
      case "app_action": {
        this.handleAppAction(r.path || r.name || "");
        break;
      }
      case "slash_command": {
        this.insertPromptText(r.name ?? "");
        break;
      }
      case "plugin_search": {
        // Hand the activated entry back to the contributing plugin through
        // the unified plugin channel; the plugin decides what "open" means.
        const owner = (r as SearchResultEntry & { plugin?: string }).plugin || this.activePlugin;
        sendPluginUiAction("search_mode", owner, "open", {
          entry: { name: r.name, snippet: r.snippet, preview: r.preview, path: r.path, icon: r.icon },
        });
        break;
      }
      default:
        this.insertPromptText(r.snippet);
    }
  }

  private handleAppAction(actionId: string): void {
    if (commandActions[actionId]) {
      commandActions[actionId]();
      return;
    }
    switch (actionId) {
      case "new_session": {
        const newTaskBtn = document.querySelector('.nav-item[data-view="chat"]') as HTMLElement;
        newTaskBtn?.click();
        break;
      }
      case "iwork_mode": {
        const segItem = document.querySelector<HTMLElement>('#mode-seg .seg-item[data-mode="iwork"]');
        segItem?.click();
        break;
      }
      case "tools_panel": {
        const btn = document.getElementById("btn-tools-trigger") as HTMLElement;
        btn?.click();
        break;
      }
      case "normal_mode":
      default: {
        const segItem = document.querySelector<HTMLElement>('#mode-seg .seg-item[data-mode="normal"]');
        if (segItem?.classList.contains("active")) segItem.click();
        break;
      }
    }
  }

  private openSettingsPanel(panel: string): void {
    window.dispatchEvent(new CustomEvent("open-settings-panel", { detail: { panel } }));
  }

  private insertPromptText(text: string): void {
    const input = document.getElementById("prompt-input") as HTMLElement;
    if (input) {
      input.textContent = text;
      input.focus();
    }
  }

  private async activateFileResult(fullPath: string, wsPath?: string): Promise<void> {
    if (wsPath) {
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "open_workspace", path: wsPath, request_id: requestId });
    }
    if (window.electronAPI) {
      try {
        const result = await window.electronAPI.readFile(fullPath);
        if (result?.content && result.size > 0) {
          const name = fullPath.split("/").pop() ?? fullPath;
          addAttachments([{
            name,
            path: fullPath,
            content: result.content,
            mime_type: result.mime_type || "",
            size: result.size,
            is_binary: result.is_binary,
          }]);
        }
      } catch (e) {
        console.warn("[search] readFile failed:", e);
      }
    }
    this.insertPromptText(t("search.readFile", { path: fullPath }));
  }

  /** Renders the plugin-mode chip row (empty string when no modes exist). */
  private pluginModesHtml(): string {
    if (this.pluginModes.length === 0) return "";
    const chips = this.pluginModes.map((m) => {
      const active = m.plugin === this.activePlugin && m.id === this.activeModeId;
      return `<button type="button" class="search-plugin-mode-chip${active ? " active" : ""}" data-plugin="${this.esc(m.plugin)}" data-mode="${this.esc(m.id)}" title="${this.esc(m.description || m.label)}">${this.esc(m.label)}</button>`;
    }).join("");
    return `<div class="search-plugin-modes">${chips}</div>`;
  }

  /** Renders the merged backend + local results grouped into sections. */
  private renderResults(): void {
    // Keyed scheduler + shared boundary: several pushes in one frame coalesce,
    // and a failure leaves the previous result list in place.
    scheduleRender("search", () => {
      guardRender("search", "results", () => this._renderResultsImpl());
    }, 60);
  }

  private _renderResultsImpl(): void {
    const q = this.input.value.trim();
    const filter = getState().searchFilter;
    const modeActive = !!this.activeModeId;
    const chipsHtml = this.pluginModesHtml();
    // A newer backend result sequence landing ends the in-flight state.
    const appliedSeq = getAppliedSearchSeq();
    if (appliedSeq > this.lastAppliedSeq) {
      this.lastAppliedSeq = appliedSeq;
      this.searching = false;
    }
    // While a request is in flight, ignore stale backend results so they
    // never mismatch the current query.
    const backendResults = modeActive || this.searching
      ? []
      : getState().searchResults.filter((r) => {
          if (r.kind === "conversation") return filter.session;
          if (r.kind === "file") return filter.workspace_file;
          return true;
        });
    const localResults = modeActive ? [] : runLocalSearch(q, filter);
    const results = modeActive ? this.pluginResults : this.mergeResults(backendResults, localResults);
    this.currentResults = results;

    if (results.length === 0) {
      // No hits at all: show either a pure spinner (backend scan in flight)
      // or the no-results empty state. Never mix text + spinner, and once a
      // single result exists the spinner is gone entirely.
      if (q && this.searching) {
        this.resultsEl.innerHTML = `${chipsHtml}<div class="search-loading-spinner"><i data-lucide="loader-circle" class="lucide search-loading-spinner-icon"></i></div>`;
        if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: this.resultsEl });
        return;
      }
      this.resultsEl.innerHTML = q
        ? `${chipsHtml}<div class="si-empty-center" style="min-height:0;padding:32px 16px"><i data-lucide="search-x" class="lucide"></i><span class="si-empty-title">${t("search.noResultsFor", { query: this.esc(q) })}</span></div>`
        : chipsHtml;
      if (q && typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: this.resultsEl });
      return;
    }

    // In a plugin mode the results render as one section owned by the mode;
    // otherwise the standard kind-based sections apply.
    const mode = modeActive
      ? this.pluginModes.find((m) => m.plugin === this.activePlugin && m.id === this.activeModeId)
      : undefined;
    const sections = modeActive && mode
      ? [{
          getLabel: () => mode.label,
          icon: mode.icon && isKnownLucideIcon(mode.icon)
            ? `<i data-lucide="${this.esc(mode.icon)}" class="lucide lucide-sm"></i>`
            : `<i data-lucide="layout-grid" class="lucide lucide-sm"></i>`,
          kindLabel: () => mode.label,
          items: results,
        }]
      : this.buildSections(results);

    let html = `${chipsHtml}<div class="search-result-count">${t("search.resultCount", { count: results.length })}</div>`;
    for (const sec of sections) {
      if (sec.items.length === 0) continue;
      html += `<div class="search-result-section-title">${sec.getLabel()}</div>`;
      for (const r of sec.items) {
        const idx = results.indexOf(r);
        const sel = idx === this.selectedIdx ? " selected" : "";
        const kindLabel = this.esc(sec.kindLabel(r));
        const snippet = this.esc(r.snippet);
        const preview = r.preview ? `<span class="search-result-preview">${this.esc(r.preview)}</span>` : "";
        const itemIcon = r.kind === "file"
          ? `<img class="search-result-file-icon" src="${getFileIcon(r.name || (r.snippet || "").split("/").pop() || "")}" alt="" draggable="false">`
          : r.icon && isKnownLucideIcon(r.icon)
            ? `<i data-lucide="${this.esc(r.icon)}" class="lucide lucide-sm"></i>`
            : sec.icon;
        html += `<div class="search-result-item${sel}" data-idx="${idx}">
          ${itemIcon}
          <div class="search-result-body">
            <span class="search-result-kind">${kindLabel}</span>
            <span class="search-result-snippet">${snippet}</span>
            ${preview}
          </div>
        </div>`;
      }
    }
    // No spinner/loading row here: as soon as one result exists we render it
    // cleanly and never keep showing a loading animation on top.

    this.resultsEl.innerHTML = html;
    this.bindClicks();

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.resultsEl });
    }
  }

  /** Runs the shared local search (MiniSearch-backed) for the given query. */
  private searchLocal(q: string): SearchResultEntry[] {
    return runLocalSearch(q, getState().searchFilter);
  }

  /** Merges backend and local results, de-duplicated by kind+identity, session_id,
   *  and cross-kind navigation duplicates. */
  private mergeResults(backend: SearchResultEntry[], local: SearchResultEntry[]): SearchResultEntry[] {
    const seen = new Set<string>();
    const seenSessionIds = new Set<string>();
    const merged = [...backend];
    for (const item of backend) {
      seen.add(`${item.kind}:${item.session_id || item.path || item.name || item.snippet}`);
      if (item.session_id) seenSessionIds.add(item.session_id);
    }
    for (const item of local) {
      const key = `${item.kind}:${item.path || item.name || item.session_id || item.snippet}`;
      if (item.session_id && seenSessionIds.has(item.session_id)) continue;
      if (!seen.has(key)) {
        merged.push(item);
        seen.add(key);
        if (item.session_id) seenSessionIds.add(item.session_id);
      }
    }

    // Cross-kind dedup: a result is redundant when it is just another route to
    // a destination already covered by a different-kind result in the list.
    const navPanels = new Set<string>();
    const slashCommandNames = new Set<string>();
    for (const r of merged) {
      if (r.kind === "settings_nav" && r.preview) navPanels.add(r.preview);
      if (r.kind === "slash_command" && r.name) slashCommandNames.add(r.name.replace(/^\//, "").toLowerCase());
    }
    return merged.filter((r) => {
      // app_action that opens a settings panel already reachable via settings_nav.
      if (r.kind === "app_action" && r.path) {
        const panel = ACTION_TO_NAV_PANEL[r.path];
        if (panel && navPanels.has(panel)) return false;
      }
      // Custom commands are also registered as slash commands with the same
      // name, so a match would show each command under two sections.
      if (r.kind === "custom_command" && r.name && slashCommandNames.has(r.name.toLowerCase())) return false;
      return true;
    });
  }

  /** Groups results into ordered, labeled sections for rendering. */
  private buildSections(results: SearchResultEntry[]): SectionDef[] {
    const sections: SectionDef[] = [];
    const seenKinds = new Set<string>();
    for (const kind of SECTION_ORDER) {
      const items = results.filter((r) => {
        if (kind === "session_header") return r.kind === "session_header" || r.kind === "conversation";
        return r.kind === kind;
      });
      if (items.length === 0) continue;
      const def = SECTION_DEFS[kind] || SECTION_DEFS["file"];
      sections.push({
        ...def,
        items,
      });
      seenKinds.add(kind);
      if (kind === "session_header") seenKinds.add("conversation");
    }
    const other = results.filter((r) => !seenKinds.has(r.kind));
    if (other.length) {
      const fileDef = SECTION_DEFS["file"]!;
      sections.push({
        ...fileDef,
        items: other,
      });
    }
    return sections;
  }

  private bindClicks(): void {
    this.resultsEl.querySelectorAll(".search-result-item").forEach((item) => {
      item.addEventListener("click", () => {
        const idx = parseInt((item as HTMLElement).dataset.idx || "-1");
        if (idx >= 0 && idx < this.currentResults.length) {
          this.activateResult(this.currentResults[idx]);
        }
      });
    });
  }

  private scrollToSelected(): void {
    const sel = this.resultsEl.querySelector(".search-result-item.selected");
    sel?.scrollIntoView({ block: "nearest" });
  }

  private esc(s: string): string {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }
}
