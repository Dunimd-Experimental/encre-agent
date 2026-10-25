/**
 * What data *can* exist, and how it is grouped.
 *
 * Kept separate from `provider.ts` (what data *does* exist): the registry is
 * static schema — categories, and the modules inside them — while the
 * numbers are dynamic. Adding a module means adding an id to a category's
 * `items` here plus a {@link ModuleMeta} entry; the pages never change.
 *
 * The four categories follow the real layout of `~/.dunimd/encre`: browsers
 * persist their own encrypted stores, logs are their own subtree, runtime holds
 * sessions/workspaces, and system holds keys/rollback/policy/cache.
 *
 * Deliberately absent: model, agent, MCP, skill, command, gateway, memory and
 * rules all live under the **settings** panel, and the library index is not a
 * data store. The data center must not reimplement any of them.
 *
 * Colours reuse the project's existing accent palette — the same hues the
 * agent-mode cards and status colours already use — so the chart reads as
 * part of the app rather than as a foreign widget. Category icons are one
 * lucide line family at one size: globe (browser), file-clock (logs),
 * cpu (runtime), server (system).
 */

import type { CategoryDef, ColumnDef, ModuleMeta } from "./types.js";

/**
 * Column sets for the record tables -- one per module whose core row reader
 * has landed. They feed the shared table (`list.ts::buildList`), which maps
 * every `t` onto a `.dc--*` format class and every `w` onto a `.dc--*` track.
 *
 * A module without an entry here has no reader yet, so it cannot show
 * records; the category page still navigates to it, and the page has to say
 * so honestly rather than invent a table.
 */
const col = (
  k: string,
  labelKey: string,
  t?: ColumnDef["t"],
  w?: ColumnDef["w"],
): ColumnDef => ({ k, labelKey, t, w });

const COOKIE_COLUMNS: ColumnDef[] = [
  col("name", "datacenter.col.name", "strong"),
  col("value", "datacenter.col.value", "mono"),
  col("domain", "datacenter.col.domain", "text", "mid"),
  col("expires", "datacenter.col.expires", "num", "date"),
  col("secure", "datacenter.col.secure", "bool", "flag"),
  col("httpOnly", "datacenter.col.httpOnly", "bool", "flag"),
];

const HISTORY_COLUMNS: ColumnDef[] = [
  col("title", "datacenter.col.title", "strong"),
  col("url", "datacenter.col.url", "mono"),
];

const BOOKMARK_COLUMNS: ColumnDef[] = [
  col("name", "datacenter.col.name", "strong"),
  col("url", "datacenter.col.url", "mono"),
  col("folder", "datacenter.col.folder", "text", "mid"),
  col("dateAdded", "datacenter.col.dateAdded", "num", "date"),
];

const LOGIN_COLUMNS: ColumnDef[] = [
  col("origin", "datacenter.col.origin", "text"),
  col("username", "datacenter.col.username", "text"),
  col("kind", "datacenter.col.kind", "text", "mid"),
  col("password", "datacenter.col.password", "secret", "mid"),
];

/* Site permissions are session state in the Electron main process, not a
   file under the data root -- see `CoreProvider.fetchRows`. */
const PERMS_COLUMNS: ColumnDef[] = [
  col("origin", "datacenter.col.origin", "text"),
  col("permission", "datacenter.col.permission", "strong"),
  col("granted", "datacenter.col.granted", "bool", "flag"),
];

const STORAGE_COLUMNS: ColumnDef[] = [
  col("origin", "datacenter.col.origin", "text"),
  col("key", "datacenter.col.key", "strong"),
  col("value", "datacenter.col.value", "mono"),
];

export const CATEGORIES: CategoryDef[] = [
  {
    id: "browser",
    nameKey: "datacenter.category.browser",
    noteKey: "datacenter.category.browserNote",
    icon: "globe",
    color: "#3b82f6",
    fg: "#ffffff",
    items: ["logins", "cookies", "storage", "bookmarks", "history", "perms"],
  },
  {
    id: "logs",
    nameKey: "datacenter.category.logs",
    noteKey: "datacenter.category.logsNote",
    icon: "file-clock",
    color: "#f59e0b",
    fg: "#ffffff",
    items: ["runlog", "events", "telemetry", "audit", "evolution", "fillaudit"],
  },
  {
    id: "runtime",
    nameKey: "datacenter.category.runtime",
    noteKey: "datacenter.category.runtimeNote",
    icon: "cpu",
    color: "#22c55e",
    fg: "#ffffff",
    items: ["sessions", "workspaces", "subagents", "iclaw", "autohistory", "jobs"],
  },
  {
    id: "system",
    nameKey: "datacenter.category.system",
    noteKey: "datacenter.category.systemNote",
    icon: "server",
    color: "#8b5cf6",
    fg: "#ffffff",
    items: ["keyfile", "rollback", "safety", "device", "bridge", "cache"],
  },
];

export const categoryById = (id: string): CategoryDef | undefined =>
  CATEGORIES.find((c) => c.id === id);

/**
 * Static metadata for every module, keyed by id. The category pages render
 * their module lists from `CategoryDef.items` + this map; the numbers come
 * from the provider. Icons are one lucide line family at one size.
 */
export const MODULES: Record<string, ModuleMeta> = {
  // browser
  logins: { id: "logins", labelKey: "datacenter.module.logins", icon: "key", columns: LOGIN_COLUMNS, filter: "origin" },
  cookies: { id: "cookies", labelKey: "datacenter.module.cookies", icon: "cookie", columns: COOKIE_COLUMNS, filter: "domain" },
  storage: { id: "storage", labelKey: "datacenter.module.storage", icon: "database", columns: STORAGE_COLUMNS, filter: "origin" },
  bookmarks: { id: "bookmarks", labelKey: "datacenter.module.bookmarks", icon: "bookmark", columns: BOOKMARK_COLUMNS, filter: "folder" },
  history: { id: "history", labelKey: "datacenter.module.history", icon: "history", columns: HISTORY_COLUMNS },
  perms: { id: "perms", labelKey: "datacenter.module.perms", icon: "shield", columns: PERMS_COLUMNS, filter: "permission" },
  // logs
  runlog: { id: "runlog", labelKey: "datacenter.module.runlog", icon: "scroll-text" },
  events: { id: "events", labelKey: "datacenter.module.events", icon: "activity" },
  telemetry: { id: "telemetry", labelKey: "datacenter.module.telemetry", icon: "gauge" },
  audit: { id: "audit", labelKey: "datacenter.module.audit", icon: "scale" },
  evolution: { id: "evolution", labelKey: "datacenter.module.evolution", icon: "sparkles" },
  fillaudit: { id: "fillaudit", labelKey: "datacenter.module.fillaudit", icon: "wand-sparkles" },
  // runtime
  sessions: { id: "sessions", labelKey: "datacenter.module.sessions", icon: "message-square" },
  workspaces: { id: "workspaces", labelKey: "datacenter.module.workspaces", icon: "folder" },
  subagents: { id: "subagents", labelKey: "datacenter.module.subagents", icon: "users" },
  iclaw: { id: "iclaw", labelKey: "datacenter.module.iclaw", icon: "inbox" },
  autohistory: { id: "autohistory", labelKey: "datacenter.module.autohistory", icon: "clock" },
  jobs: { id: "jobs", labelKey: "datacenter.module.jobs", icon: "list" },
  // system
  keyfile: { id: "keyfile", labelKey: "datacenter.module.keyfile", icon: "lock" },
  rollback: { id: "rollback", labelKey: "datacenter.module.rollback", icon: "rotate-ccw" },
  safety: { id: "safety", labelKey: "datacenter.module.safety", icon: "shield" },
  device: { id: "device", labelKey: "datacenter.module.device", icon: "monitor" },
  bridge: { id: "bridge", labelKey: "datacenter.module.bridge", icon: "plug" },
  cache: { id: "cache", labelKey: "datacenter.module.cache", icon: "box" },
};
