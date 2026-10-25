/**
 * The run-log viewer: the table the Encre Agent Desktop `logs` page renders,
 * extracted so the data center's 运行日志 module can show the *same* view
 * instead of a second copy of it.
 *
 * It owns its subtree inside the host element and hands back `destroy()`,
 * because a data-center module page mounts and tears this down on every
 * navigation — the document-level listeners and the refresh poll must not
 * outlive the view they belong to.
 *
 * Log lines come from the main process (`electronAPI.getLogs`), which reads
 * and parses `yimd.log` itself; batches of 500 are appended as the body
 * scrolls, and a 5s size poll reloads from the top when the file changes.
 */

import { t } from "../features/i18n.js";
import { EALoader } from "../features/ealoader.js";
import { pluginMenuSpecs } from "../features/plugin_contributions.js";
import {
  clearContextTargets,
  renderContextMenuItems,
  showContextMenu,
} from "./context-menu.js";
import { hideAnimated } from "./animate.js";

/** One parsed log line, as the main process returns it. */
interface LogEntry {
  timestamp: string;
  level: string;
  source: string;
  message: string;
}

const BATCH = 500;
const REFRESH_MS = 5000;

function esc(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function paintIcons(root: HTMLElement): void {
  const lucide = (window as unknown as { lucide?: { createIcons?: (o: { root: HTMLElement }) => void } }).lucide;
  lucide?.createIcons?.({ root });
}

export interface LogViewHandle {
  destroy(): void;
}

export function mountLogView(host: HTMLElement): LogViewHandle {
  host.innerHTML = `
    <div class="log-view">
      <div class="log-table">
        <div class="log-table-header">
          <div class="log-cell log-cell-ts">${t("settings.aboutLogTimestamp")}</div>
          <div class="log-cell log-cell-lvl">${t("settings.aboutLogLevel")}</div>
          <div class="log-cell log-cell-src">${t("settings.aboutLogSource")}</div>
          <div class="log-cell log-cell-msg">${t("settings.aboutLogMessage")}</div>
        </div>
        <div class="log-table-body"></div>
      </div>
    </div>
    <div class="context-menu hidden"></div>`;
  paintIcons(host);

  const bodyEl = host.querySelector<HTMLElement>(".log-table-body");
  const ctxMenu = host.querySelector<HTMLElement>(".context-menu");
  if (!bodyEl || !ctxMenu) return { destroy(): void {} };
  // Branded loading state while the first batch is read.
  EALoader.mountCentered(bodyEl, "96px");

  let allEntries: LogEntry[] = [];
  let destroyed = false;

  const emptyState = (): void => {
    bodyEl!.innerHTML = `<div class="si-empty-center"><i data-lucide="scroll-text" class="lucide"></i><span class="si-empty-title">${t("settings.aboutLogNoLogs")}</span></div>`;
    paintIcons(bodyEl!);
  };

  const render = (): void => {
    hideAnimated(ctxMenu);
    // The first render replaces the initial loading state.
    EALoader.destroyMounted(bodyEl!);
    if (allEntries.length === 0) {
      emptyState();
      return;
    }
    bodyEl!.innerHTML = allEntries.map((e) => {
      const levelClass = "log-lvl-" + e.level.toLowerCase();
      const ts = e.timestamp.replace(/,\d{3}$/, "");
      return `<div class="log-row">
        <div class="log-cell log-cell-ts">${ts}</div>
        <div class="log-cell log-cell-lvl"><span class="log-level-badge ${levelClass}">${e.level}</span></div>
        <div class="log-cell log-cell-src">${esc(e.source)}</div>
        <div class="log-cell log-cell-msg"><span class="log-msg-text">${esc(e.message)}</span></div>
      </div>`;
    }).join("");
  };

  // ── Right-click: copy this row / copy everything ───────────────────
  const onCtx = (ev: MouseEvent): void => {
    if (destroyed) return;
    const row = (ev.target as HTMLElement).closest(".log-row");
    if (!row || !bodyEl!.contains(row)) return;
    const idx = Array.from(bodyEl!.querySelectorAll(".log-row")).indexOf(row);
    const entry = allEntries[idx];
    if (!entry) return;
    const line = (e: LogEntry): string => `[${e.timestamp}] [${e.level}] ${e.source}: ${e.message}`;
    renderContextMenuItems(
      [
        {
          action: "copy-row",
          icon: "copy",
          label: t("settings.aboutLogCopyRow"),
          onClick: () => void navigator.clipboard.writeText(line(entry)).catch(() => {}),
        },
        {
          action: "copy-all",
          icon: "files",
          label: t("settings.aboutLogCopyAll"),
          onClick: () => void navigator.clipboard.writeText(allEntries.map(line).join("\n")).catch(() => {}),
        },
        ...pluginMenuSpecs("logs"),
      ],
      { container: ctxMenu },
    );
    // Keep the right-clicked row highlighted while the menu is open — hover
    // alone fades out as soon as the pointer moves onto the menu.
    showContextMenu(ctxMenu, ev.clientX, ev.clientY, { target: row as HTMLElement });
    ev.preventDefault();
  };
  const onDocClick = (): void => {
    if (destroyed) return;
    hideAnimated(ctxMenu);
    clearContextTargets();
  };
  document.addEventListener("contextmenu", onCtx);
  document.addEventListener("click", onDocClick, false);

  // ── Windowed loading + refresh poll ────────────────────────────────
  let loadedCount = 0;
  let total = 0;
  let loading = false;
  let poll: number | undefined;

  const api = (window as unknown as {
    electronAPI?: {
      getLogs?: (q: { offset: number; limit: number }) => Promise<{ entries: LogEntry[]; total: number }>;
      getLogFileInfo?: () => Promise<{ exists: boolean; size: number }>;
    };
  }).electronAPI;

  const loadMore = async (): Promise<void> => {
    if (!api?.getLogs || loading) return;
    if (total > 0 && loadedCount >= total) return;
    loading = true;
    try {
      const result = await api.getLogs({ offset: loadedCount, limit: BATCH });
      if (result.entries && result.entries.length > 0) {
        allEntries.push(...result.entries);
        loadedCount += result.entries.length;
        total = result.total;
        render();
      }
    } catch {
      if (loadedCount === 0) emptyState();
    }
    loading = false;
  };

  void loadMore();

  const onScroll = (): void => {
    const scrollBottom = bodyEl!.scrollTop + bodyEl!.clientHeight;
    if (scrollBottom >= bodyEl!.scrollHeight - 200) void loadMore();
  };
  bodyEl.addEventListener("scroll", onScroll);

  if (api?.getLogFileInfo) {
    let lastSize = -1;
    poll = window.setInterval(async () => {
      if (destroyed) return;
      if (!document.body.contains(bodyEl)) {
        if (poll !== undefined) window.clearInterval(poll);
        return;
      }
      if (document.hidden || loading) return;
      try {
        const info = await api.getLogFileInfo!();
        if (!info.exists) return;
        if (lastSize >= 0 && info.size === lastSize) return;
        lastSize = info.size;
        // The file changed — reload from the top.
        loading = true;
        const result = await api.getLogs!({ offset: 0, limit: BATCH });
        if (result && result.entries) {
          allEntries = result.entries;
          loadedCount = result.entries.length;
          total = result.total;
          render();
        }
        loading = false;
      } catch {
        loading = false;
      }
    }, REFRESH_MS);
  }

  return {
    destroy(): void {
      destroyed = true;
      document.removeEventListener("contextmenu", onCtx);
      document.removeEventListener("click", onDocClick, false);
      bodyEl.removeEventListener("scroll", onScroll);
      if (poll !== undefined) window.clearInterval(poll);
      host.textContent = "";
    },
  };
}
