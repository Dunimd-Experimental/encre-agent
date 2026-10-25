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
 * Automation (scheduled tasks) panel.
 *
 * Implements the "Automation" view: a tabbed UI for viewing configured
 * scheduled jobs and their run history, creating new jobs from templates,
 * filtering history, viewing a job's result, and cancelling runs. It talks to
 * the backend through the stream-layer automation callbacks and the WebSocket.
 */

import { t, getLocale, getIntlLocale, onLocaleChange } from "../features/i18n.js";
import { send } from "../core/ws.js";
import { Dialog } from "../ui/dialog.js";
import { PERMISSION_MODES, permissionModeGroup, permissionModeLabel, permissionModeIcon, guardPermissionMode, dialogPermissionConfirm } from "../core/permission_mode.js";
import { getState, subscribe, restoreMessages, setAutomationHistory, locateModelInProviders } from "../core/state.js";
import { scheduleRender } from "../core/render_scheduler.js";
import { guardRender } from "../core/ui_boundary.js";
import { showSessionContextMenu, showRenameDialog } from "./session.js";
import { clampIntoViewport, showContextMenu, renderContextMenuItems } from "../ui/context-menu.js";
import { pluginMenuSpecs } from "../features/plugin_contributions.js";
import { removeAnimated } from "../ui/animate.js";
import {
  onAutomationJobCreated,
  onAutomationJobUpdated,
  onAutomationJobCancelled,
  downloadMarkdownFile,
} from "../core/stream.js";
import { renderMarkdown, Chat } from "../chat/chat.js";
import { platformIconHtml } from "../ui/icons.js";
import { attachInputExpand } from "../ui/input-expand.js";
import type { Message } from "../core/types.js";

interface TaskTemplate {
  id: string;
  icon: string;
  titleKey: string;
  descKey: string;
  defaultNameKey: string;
  defaultPrompt: string;
  defaultCron: string;
}

interface BackendJob {
  id: string;
  name: string;
  prompt: string;
  cron: string;
  schedule_type: string;
  state: string;
  suspended: boolean;
  created_at: number;
  last_fired: number | null;
  last_result: string | null;
  fail_count: number;
  max_failures: number;
  tag: string;
  model_index: number;
  /** Workspace path the job is pinned to; empty/absent keeps the default sandbox. */
  workspace?: string;
  /** Permission preset the job runs under; empty/absent keeps the ambient agent permission. */
  permission_mode?: string;
  push_gateways?: string[];
}

interface HistoryEntry {
  id: string;
  job_id: string;
  name: string;
  prompt?: string;
  tag: string;
  time: number;
  state: string;
  last_result: string;
  fail_count: number;
  session_id?: string;
  messages?: any[];
  error_code?: string;
}

interface AutomationViewResult {
  id: string;
  job_id: string;
  name: string;
  prompt: string;
  result: string;
  tag: string;
  state: string;
  session_id?: string;
  messages: any[];
  error_code?: string;
}

/** Localized gateway name matching the label used in Settings → Gateway. */
function gatewayDisplayName(id: string): string {
  const key = "settings.adapterName" + id.charAt(0).toUpperCase() + id.slice(1);
  const name = t(key);
  return name === key ? id : name;
}

const TEMPLATES: TaskTemplate[] = [
  {
    id: "ai-news",
    icon: "newspaper",
    titleKey: "automation.templateAiNewsTitle",
    descKey: "automation.templateAiNewsDesc",
    defaultNameKey: "defaultNameAiNews",
    defaultPrompt:
      "You are an AI industry analyst. Search and summarize today's important AI developments across these dimensions:\n\n1. 🚀 Product Launches & Feature Updates\n   - Updates from OpenAI, Google, Anthropic, Meta, and other major labs\n   - New model releases, API updates, product iterations\n\n2. 💰 Funding & Business\n   - Key funding rounds (amount, stage, investors)\n   - M&A and strategic partnerships\n\n3. 🔬 Research Breakthroughs\n   - Notable papers and technical reports\n   - Open-source project updates\n\n4. 📋 Governance & Standards\n   - AI safety framework changes\n   - Regulatory developments\n\nOutput format:\n- Rank by importance, 5-8 items\n- Each item: title, 1-2 sentence summary, source link",
    defaultCron: "0 9 * * 1-5",
  },
  {
    id: "brand-monitor",
    icon: "eye",
    titleKey: "automation.templateBrandMonitorTitle",
    descKey: "automation.templateBrandMonitorDesc",
    defaultNameKey: "defaultNameBrandMonitor",
    defaultPrompt:
      "You are a brand reputation analyst. Monitor the latest mentions and sentiment about the specified brand across social media and communities.\n\nPlatforms: Weibo, Zhihu, Twitter, Reddit\n\nOutput format:\n\n1. 📊 Sentiment Breakdown\n   - Positive / Negative / Neutral percentages\n   - Overall trend\n\n2. 📌 Key Mentions Summary\n   - High-traffic discussions (platform, summary, engagement metrics)\n   - Influencer/key opinion comments\n\n3. ⚠️ Risk Alerts\n   - Potential PR incidents\n   - Rising negative sentiment trends\n\n4. Recommendations\n   - Suggested response strategies based on current sentiment",
    defaultCron: "0 9 * * 1",
  },
  {
    id: "competitor-track",
    icon: "target",
    titleKey: "automation.templateCompetitorTitle",
    descKey: "automation.templateCompetitorDesc",
    defaultNameKey: "defaultNameCompetitorTrack",
    defaultPrompt:
      "You are a competitive intelligence analyst. Track the latest developments of specified competitors and produce a monitoring report.\n\nOutput format:\n\n1. 🆕 Product Changes\n   - New features (version, release date)\n   - UI/UX changes\n   - Pricing adjustments\n\n2. 💬 Community & User Feedback\n   - User sentiment trends across platforms\n   - Common complaints and feature requests\n   - Rating changes\n\n3. 📈 Market Dynamics\n   - Market share shifts\n   - Media coverage highlights\n   - Hiring and strategic moves\n\n4. ⚡ Impact Assessment\n   - Potential impact on our product/brand\n   - Recommended countermeasures",
    defaultCron: "0 10 * * 1",
  },
  {
    id: "stock-monitor",
    icon: "trending-up",
    titleKey: "automation.templateStockTitle",
    descKey: "automation.templateStockDesc",
    defaultNameKey: "defaultNameStockMonitor",
    defaultPrompt:
      "You are a financial risk analyst. Monitor the latest price movements and anomalies in the watchlist and produce an alert report.\n\nOutput format:\n\n1. 📊 Price Movement Summary\n   - Top 5 gainers / losers\n   - Volume anomalies\n\n2. ⚠️ Anomaly Analysis\n   - Stocks exceeding daily volatility thresholds\n   - Unusual trading patterns\n   - Possible triggers (earnings, news, macro factors)\n\n3. 📰 Related News\n   - Key news affecting stock prices\n   - Policy or regulatory changes\n\n4. 💡 Risk Assessment\n   - Current portfolio risk score\n   - Items requiring attention",
    defaultCron: "0 */1 * * 1-5",
  },
  {
    id: "security-scan",
    icon: "shield",
    titleKey: "automation.templateSecurityTitle",
    descKey: "automation.templateSecurityDesc",
    defaultNameKey: "defaultNameSecurityScan",
    defaultPrompt:
      "You are a senior security engineer. Scan the code repository for verified high and medium severity security vulnerabilities.\n\nOutput format:\n\n1. 🔴 High Severity Vulnerabilities (descending by severity)\n   - Vulnerability type (SQL injection, XSS, RCE, privilege escalation, etc.)\n   - Affected files and line numbers\n   - Risk level (CVSS score)\n\n2. 🟡 Medium Severity Vulnerabilities\n   - Same format as above\n\n3. 🔧 Fix Recommendations\n   - Concrete fix for each vulnerability\n   - Code change examples\n\n4. 🔗 References\n   - CVE IDs (if applicable)\n   - Related security advisories\n   - Best practice documentation",
    defaultCron: "0 */3 * * *",
  },
  {
    id: "bug-scan",
    icon: "bug",
    titleKey: "automation.templateBugScanTitle",
    descKey: "automation.templateBugScanDesc",
    defaultNameKey: "defaultNameBugScan",
    defaultPrompt:
      "You are a senior code review engineer. Analyze recent commits and identify high-risk changes that could introduce critical bugs.\n\nOutput format:\n\n1. 🐛 Bug Description\n   - Risk type (null pointer, resource leak, concurrency, logic error, etc.)\n   - Related commits (hash + author)\n   - Affected files and functions\n\n2. 📐 Impact Scope\n   - Trigger conditions\n   - Potentially affected users/modules\n   - Severity assessment\n\n3. 🛠️ Fix Recommendations\n   - Specific fix code examples\n   - Verification steps\n   - Whether hotfix is needed",
    defaultCron: "0 */2 * * *",
  },
  {
    id: "test-coverage",
    icon: "flask-conical",
    titleKey: "automation.templateTestCoverageTitle",
    descKey: "automation.templateTestCoverageDesc",
    defaultNameKey: "defaultNameTestCoverage",
    defaultPrompt:
      "You are a QA engineer. Identify high-risk areas in recent code changes that lack test coverage and generate suggested test cases.\n\nOutput format:\n\n1. 🎯 Uncovered Code Analysis\n   - Functions/methods missing tests\n   - Cyclomatic complexity assessment\n   - Risk level\n\n2. ✅ Suggested Test Cases\n   - Test scenarios for each high-risk area\n   - Boundary conditions and error paths\n   - Mock/stub recommendations\n\n3. 🔧 Test Implementation\n   - Ready-to-use test code snippets\n   - Required fixtures\n   - How to run",
    defaultCron: "0 9 * * 1",
  },
  {
    id: "daily-summary",
    icon: "git-commit",
    titleKey: "automation.templateDailySummaryTitle",
    descKey: "automation.templateDailySummaryDesc",
    defaultNameKey: "defaultNameDailySummary",
    defaultPrompt:
      "You are an engineering team assistant. Summarize today's code repository changes into a readable engineering daily report.\n\nOutput format:\n\n1. 📊 Commit Statistics\n   - Total commits\n   - Files changed\n   - Contributors\n\n2. 🔄 Key Changes\n   - Grouped by module/feature\n   - Each entry: author, commit message, scope\n   - Highlight breaking changes\n\n3. ⚠️ Risk Notices\n   - High-risk changes\n   - PRs pending review\n   - Rollback suggestions if needed\n\n4. 🏗️ Build & Deploy Status\n   - CI/CD pipeline status\n   - Build failures summary (if any)",
    defaultCron: "0 20 * * 1-5",
  },
];

function formatSchedule(cron: string): string {
  if (!cron) return t("automation.once");
  const parts = cron.split(" ");
  if (parts.length !== 5) return cron;
  const [min, hour, , , dow] = parts;
  if (hour === "*" && min === "*") return t("automation.everyMinute");
  if (hour !== "*" && min !== "*" && dow === "*")
    return `${hour.padStart(2, "0")}:${min.padStart(2, "0")} ${t("automation.daily")}`;
  if (dow !== "*")
    return `${hour.padStart(2, "0")}:${min.padStart(2, "0")} ${t("automation.onWeekdays")}`;
  return cron;
}

function parseCronForUI(cron: string): { scheduleType: string; time: string } {
  const parts = cron.split(" ");
  if (parts.length !== 5) return { scheduleType: "daily", time: "09:00" };
  const [min, hour, , , dow] = parts;
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
  if (hour !== "*" && min !== "*" && dow === "*")
    return { scheduleType: "daily", time };
  if (hour === "*" && min === "*")
    return { scheduleType: "hourly", time: "09:00" };
  if (hour !== "*" && min !== "*" && (dow === "1" || dow === "0" || dow === "7"))
    return { scheduleType: "weekly", time };
  return { scheduleType: "daily", time };
}

function formatDateTime(unixTs: number): string {
  const d = new Date(unixTs * 1000);
  const l = getLocale();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  if (l === "en") return `${mm}/${dd}/${yyyy} ${hh}:${mi}`;
  if (l === "zh") return `${yyyy}年${Number(mm)}月${Number(dd)}号 ${hh}:${mi}`;
  if (l === "zh-Hant") return `${yyyy}年${Number(mm)}月${Number(dd)}日 ${hh}:${mi}`;
  return d.toLocaleString(getIntlLocale(), {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function formatDate(unixTs: number): string {
  const d = new Date(unixTs * 1000);
  const l = getLocale();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  if (l === "en") return `${mm}/${dd}`;
  if (l === "zh") return `${Number(mm)}月${Number(dd)}日`;
  if (l === "zh-Hant") return `${Number(mm)}月${Number(dd)}日`;
  return d.toLocaleDateString(getIntlLocale(), { month: "2-digit", day: "2-digit" });
}

/**
 * Controller for the automation/scheduled-task panel.
 */
export class Automation {
  private el: HTMLElement;
  private tabsEl: HTMLElement;
  private panelsEl: HTMLElement;
  private containerEl: HTMLElement;
  private configuredListEl: HTMLElement;
  private historyTimelineEl: HTMLElement;
  private createWrap: HTMLElement;
  private createBtn: HTMLElement;
private createDropdown: HTMLElement;
  private activeTab = "history";
  private detailEl: HTMLElement;
  private detailContentEl: HTMLElement;
  private detailBreadcrumbEl: HTMLElement;
  private activeExecution: AutomationViewResult | null = null;
  private chatRenderer: Chat | null = null;

  /** Set the Chat instance used to render sub-agent detail timelines. */
  setChatRenderer(chat: Chat): void {
    this.chatRenderer = chat;
  }

  private jobs: BackendJob[] = [];
  // History filter state
  private historyStatus: string = "";   // "" = all, or "SUCCESS"/"FAILED"/"RUNNING"/"PENDING"
  private historyTaskId: string = "";   // "" = all, or a specific job id
  private historyDateFrom: string = ""; // YYYY-MM-DD or ""
  private historyDateTo: string = "";   // YYYY-MM-DD or ""
  private historyFiltersBound: boolean = false;
  // Rebind closures + date label elements so labels can be refreshed on locale change
  private _rebindStatus: (() => void) | null = null;
  private _rebindTask: (() => void) | null = null;
  private _dateFromText: HTMLElement | null = null;
  private _dateToText: HTMLElement | null = null;
  private _syncDateClearVisibility: (() => void) | null = null;
  // Cached filtered history for export (set by renderHistory)
  private currentFilteredHistory: HistoryEntry[] = [];

  /**
   * Constructor: resolves DOM nodes and wires tabs, create button and filters.
   */
  constructor() {
    this.el = document.getElementById("automation-view")!;
    this.tabsEl = this.el.querySelector(".automation-tabs")! as HTMLElement;
    this.panelsEl = this.el.querySelector(".automation-panels")! as HTMLElement;
    this.containerEl = this.el.querySelector(".automation-container")! as HTMLElement;
    this.configuredListEl = document.getElementById("configured-list")!;
    this.historyTimelineEl = document.getElementById("history-timeline")!;
    this.createWrap = document.getElementById("automation-create-wrap")!;
    this.createBtn = document.getElementById("automation-create-btn")!;
    this.createDropdown = document.getElementById("automation-create-dropdown")!;
    this.detailEl = this.el.querySelector(".automation-detail")! as HTMLElement;
    this.detailContentEl = document.getElementById("automation-detail-content")!;
    this.detailBreadcrumbEl = document.getElementById("automation-detail-breadcrumb")!;

    this.bindTabs();
    this.bindCallbacks();
    this.bindCreateButton();
    this.bindHistoryFilters();

    // Re-render history filter labels/dropdowns when the locale changes so
    // they track the current language instead of the one active at bind time.
    onLocaleChange(() => this.updateHistoryFilterLabels());

    // Close any open history dropdowns when clicking outside
    document.addEventListener("click", (e) => {
      const target = e.target as Node;
      const filtersEl = document.getElementById("history-filters");
      if (filtersEl && !filtersEl.contains(target)) {
        document.querySelectorAll("#history-filters .settings-dropdown.open").forEach((dd) => {
          dd.classList.remove("open");
        });
      }
    });

    // History rows: one delegated listener each for activate / context menu.
    // Bound once here (not per item inside `renderHistory`) so no re-render can
    // ever drop them — see the note at the end of `renderHistory`.
    this.historyTimelineEl.addEventListener("click", (e) => {
      const item = (e.target as HTMLElement).closest(".history-timeline-item") as HTMLElement | null;
      if (!item || !this.historyTimelineEl.contains(item)) return;
      this.onHistoryItemClick(item);
    });
    this.historyTimelineEl.addEventListener("contextmenu", (e: MouseEvent) => {
      const item = (e.target as HTMLElement).closest(".history-timeline-item") as HTMLElement | null;
      if (!item || !this.historyTimelineEl.contains(item)) return;
      e.preventDefault();
      this.onHistoryItemContextMenu(item, e.clientX, e.clientY);
    });

    // Re-render an open detail view when the locale changes so the
    // reused breadcrumb ("Automation / …") and empty states translate.
    onLocaleChange(() => {
      if (this.isDetailVisible() && this.activeExecution) {
        this.showDetail(this.activeExecution);
      }
    });
}

  private escapeHtml(text: string): string {
    const div = document.createElement("div");
    div.appendChild(document.createTextNode(text));
    return div.innerHTML;
  }

  // ── Detail view (sub-agent result inside automation panel) ──────

  /** Open an execution in the sub-agent detail container. */
  openExecution(data: AutomationViewResult): void {
    this.activeExecution = {
      ...data,
      messages: Array.isArray(data.messages) ? data.messages : [],
    };
    this.showDetail(this.activeExecution);
  }

  /** Apply a live event to the execution currently open. */
  updateExecution(jobId: string, eventType: string, eventData: Record<string, unknown>): void {
    if (!this.activeExecution || this.activeExecution.job_id !== jobId) return;
    if (eventType === "start") {
      this.activeExecution = { ...this.activeExecution, state: "RUNNING", name: String(eventData.name || this.activeExecution.name), prompt: String(eventData.prompt || this.activeExecution.prompt), session_id: String(eventData.session_id || this.activeExecution.session_id || "") || undefined };
    } else if (eventType === "snapshot") {
      const msgs = eventData.messages;
      this.activeExecution = { ...this.activeExecution, state: "RUNNING", messages: Array.isArray(msgs) ? msgs : this.activeExecution.messages, session_id: String(eventData.session_id || this.activeExecution.session_id || "") || undefined };
    } else if (eventType === "tool_progress" || eventType === "tool_result") {
      const msgs = eventData.sub_agent_messages;
      if (Array.isArray(msgs)) this.activeExecution = { ...this.activeExecution, state: "RUNNING", messages: msgs };
    } else if (eventType === "finish") {
      const failed = eventData.state === "FAILED" || !!eventData.error_code;
      // Keep the transcript and the raw result even on failure so the detail
      // view can render the real execution trace (and the actual error text)
      // instead of a bare "execution failed" placeholder.
      const keepMsgs = (Array.isArray(eventData.messages) && eventData.messages.length > 0)
        ? eventData.messages
        : this.activeExecution.messages;
      this.activeExecution = { ...this.activeExecution, state: failed ? "FAILED" : "COMPLETED", messages: keepMsgs, result: String(eventData.result || this.activeExecution.result || ""), error_code: failed ? String(eventData.error_code || "AUTOMATION_EXECUTION_FAILED") : undefined };
    } else { return; }
    if (this.isDetailVisible()) this.showDetail(this.activeExecution);
  }

  /** Apply a final job-update payload. */
  updateExecutionResult(data: Partial<AutomationViewResult> & { id?: string; job_id?: string; action?: string }): void {
    if (!this.activeExecution) return;
    const jobId = data.job_id || data.id;
    if (!jobId || jobId !== this.activeExecution.job_id) return;
    const failed = data.state === "FAILED" || data.action === "failed";
    this.activeExecution = { ...this.activeExecution, ...data, job_id: this.activeExecution.job_id, state: failed ? "FAILED" : (data.state || this.activeExecution.state), messages: (Array.isArray(data.messages) && data.messages.length > 0) ? data.messages : this.activeExecution.messages, result: String(data.result || this.activeExecution.result || ""), error_code: failed ? (data.error_code || "AUTOMATION_EXECUTION_FAILED") : undefined };
    if (this.isDetailVisible()) this.showDetail(this.activeExecution);
  }

  private showDetail(data: { name: string; prompt?: string; messages?: any[]; state?: string; result?: string; error_code?: string }): void {
    if (data.state === "FAILED") {
      if (data.messages && data.messages.length > 0) {
        // A failed run still carries its transcript: render the full
        // execution trace (the last assistant message shows the stamped
        // errorMessage as a red card), matching the main-agent UX.
        this.renderSubAgentTimeline(data.messages);
      } else {
        const errorCode = data.error_code || "AUTOMATION_EXECUTION_FAILED";
        const errDetail = data.result && data.result.trim() ? data.result.trim().slice(0, 500) : "";
        this.detailContentEl.innerHTML = `<div class="si-empty-center" style="flex:1;gap:14px;"><i data-lucide="ban" class="lucide" style="width:32px;height:32px;color:var(--text-muted);opacity:0.35;"></i><div class="si-empty-title">${this.escapeHtml(t("automation.executionFailed") || "Execution failed")}</div><div class="si-empty-sub">${this.escapeHtml(errorCode)}${errDetail ? `<br><span style="opacity:0.75;word-break:break-word;">${this.escapeHtml(errDetail)}</span>` : ""}</div></div>`;
        if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: this.detailContentEl });
      }
    } else if (data.messages && data.messages.length > 0) {
      this.renderSubAgentTimeline(data.messages);
    } else if (data.prompt) {
      this.renderTaskCard(data.prompt, data.state || "");
    } else if (data.state === "RUNNING" || data.state === "PENDING") {
      this.detailContentEl.innerHTML = `<div class="automation-detail-loader"><i data-lucide="loader-circle" class="lucide" style="animation:historySpin 1s linear infinite;"></i></div>`;
      if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: this.detailContentEl });
    } else {
      this.detailContentEl.innerHTML = `<div class="si-empty-center"><i data-lucide="message-square" class="lucide"></i><span class="si-empty-title">${t("automation.noMessages") || "No messages"}</span></div>`;
    }
    this.containerEl.style.display = "none";
    this.detailEl.classList.remove("hidden");
    this.detailEl.classList.add("active");
    this.tabsEl.style.display = "none";
    // Reuse the tape's session-crumb breadcrumb structure so the detail
    // header is identical to the sub-agent view: Automation / <name>.
    this.renderDetailBreadcrumb(data.name || "");
    // Detail view: show the Back button in the sidebar-toggle's slot (toggle
    // hidden). The sidebar is force-collapsed in automation, so the toggle has
    // no useful action here anyway — Back is the primary header control.
    const autoBack = document.getElementById("btn-automation-back");
    const toggle = document.getElementById("btn-toggle-sidebar");
    if (autoBack) autoBack.classList.remove("hidden");
    if (toggle) toggle.style.display = "none";
  }

  /**
   * Render the detail breadcrumb reusing the exact same session-crumb
   * markup the main chat ("tape") uses for its sub-agent view, so the
   * two are visually identical. Root crumb returns to the list.
   */
  private renderDetailBreadcrumb(name: string): void {
    const rootLabel = t("automation.title") || "Automation";
    const trunc = (s: string, max = 18) => (s.length > max ? s.slice(0, max) + "…" : s);
    this.detailBreadcrumbEl.innerHTML =
      `<button class="session-crumb session-crumb-root" type="button" data-tooltip="${this.escapeHtml(rootLabel)}">${this.escapeHtml(trunc(rootLabel))}</button>` +
      `<span class="session-crumb-sep">/</span>` +
      `<span class="session-crumb-current" data-tooltip="${this.escapeHtml(name)}">${this.escapeHtml(trunc(name))}</span>`;
    const root = this.detailBreadcrumbEl.querySelector(".session-crumb-root");
    if (root) root.addEventListener("click", () => this.hideDetail());
  }

  hideDetail(): void {
    this.detailEl.classList.remove("active");
    this.detailEl.classList.add("hidden");
    this.containerEl.style.display = "";
    this.tabsEl.style.display = "";
    this.detailContentEl.innerHTML = "";
    this.detailBreadcrumbEl.innerHTML = "";
    this.activeExecution = null;
    // Returning to the automation list view: hide the Back button. The
    // sidebar-toggle keeps styling the AutomationPanel applied on entry
    // (visible but disabled) - clearing showDetail's inline display keeps the
    // header layout stable.
    const autoBack = document.getElementById("btn-automation-back");
    const toggle = document.getElementById("btn-toggle-sidebar");
    if (autoBack) autoBack.classList.add("hidden");
    if (toggle) {
      toggle.style.display = "";
    }
  }

  isDetailVisible(): boolean {
    return this.detailEl.classList.contains("active");
  }

  private renderSubAgentTimeline(messages: any[]): void {
    if (this.chatRenderer) {
      const isRunning = this.activeExecution?.state === "RUNNING" || this.activeExecution?.state === "PENDING";
      // Automation history and live snapshots are persisted server messages,
      // while the shared renderer consumes normalized renderer Messages.
      this.chatRenderer.renderSubAgentInto(this.detailContentEl, restoreMessages(messages), !!isRunning);
    }
  }

  /**
   * Fallback detail body when an execution has no sub-agent messages yet:
   * always show the task the user originally defined (job prompt) together
   * with a consistent status line, instead of a bare infinite spinner.
   */
  private renderTaskCard(prompt: string, state: string): void {
    const pending = state === "RUNNING" || state === "PENDING";
    const icon = state === "COMPLETED" ? "circle-check" : state === "FAILED" ? "circle-x" : "loader-circle";
    const iconCls = state === "COMPLETED" ? "color:var(--success)" : state === "FAILED" ? "color:var(--danger)" : "color:var(--warning)";
    const label = state === "COMPLETED" ? t("automation.stateSuccess")
      : state === "FAILED" ? t("automation.stateFailed")
      : state === "RUNNING" ? t("automation.stateRunning")
      : t("automation.statePending");
    const spin = pending ? "animation:historySpin 1s linear infinite;" : "";
    this.detailContentEl.innerHTML = `
      <div class="automation-detail-card">
        <div class="automation-detail-status">
          <i data-lucide="${icon}" class="lucide" style="width:16px;height:16px;${iconCls};${spin}"></i>
          <span>${this.escapeHtml(label)}</span>
        </div>
        <div class="automation-detail-task-label">${this.escapeHtml(t("automation.taskContent") || "Task")}</div>
        <div class="automation-detail-task-text">${this.escapeHtml(prompt)}</div>
      </div>`;
    if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: this.detailContentEl });
  }

  // ── Rendering ───────────────────────────────────────────────────

  /** Renders the panel from the state store, without network requests.
   *
   *  The unified backend snapshot (`_push_all_data`) fills both
   *  `automationJobs` and `automationHistory` at boot and on every mode /
   *  config / workspace change, and the state subscription in
   *  {@linkcode bindCallbacks} re-renders on every subsequent plane update —
   *  so entering the view renders instantly from data that is already in
   *  memory. Live freshness is kept by the job lifecycle events
   *  (created / updated / cancelled / deleted), which re-request exactly the
   *  plane that changed. */
  render(): void {
    this.jobs = getState().automationJobs;
    // Configured jobs and run history are independent lists: a failure in
    // one must still leave the other rendered.
    guardRender("automation", "configured", () => this.renderConfigured());
    guardRender("automation", "history", () => this.renderHistory());
  }

  private bindCallbacks(): void {
    // Jobs and history both flow through global state (automationJobs /
    // automationHistory) via the unified push — no separate callback cache.
    let lastJobsRef = getState().automationJobs;
    let lastHistoryRef = getState().automationHistory;
    subscribe(() => {
      const jobs = getState().automationJobs;
      const history = getState().automationHistory;
      let dirty = false;
      if (jobs !== lastJobsRef) {
        lastJobsRef = jobs;
        this.jobs = jobs;
        this.onHistoryFiltersRebind?.();
        dirty = true;
      }
      if (history !== lastHistoryRef) {
        lastHistoryRef = history;
        this.onHistoryFiltersRebind?.();
        dirty = true;
      }
      if (dirty) scheduleRender("automation", () => this.render(), 60);
    }, ["automationJobs", "automationHistory"]);
    onAutomationJobCreated(() => {
      this.requestJobsList();
    });
    onAutomationJobUpdated(() => {
      this.requestJobsList();
    });
    onAutomationJobCancelled(() => {
      this.requestJobsList();
    });
  }

  /** Requests the list of configured automation jobs from the backend. */
  private requestJobsList(): void {
    send({ type: "automation_list_jobs" });
  }

  /** Requests the automation run history from the backend. */
  private requestHistory(): void {
    send({ type: "automation_get_history" });
  }

  /** Wires the history/top tab switching. */
  private bindTabs(): void {
    this.tabsEl.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest(".automation-tab") as HTMLElement | null;
      if (!btn) return;
      const tab = btn.getAttribute("data-automation-tab");
      if (tab && tab !== this.activeTab) {
        this.switchTab(tab);
      }
    });
  }

  private openDialog(template: TaskTemplate, editJob?: BackendJob): void {
    const isEdit = !!editJob;
    const defaultCron = editJob?.cron || template.defaultCron;
    const parsed = parseCronForUI(defaultCron);
    const title = isEdit ? t("automation.editTask") : t("automation.createTaskTitle");
    const btnLabel = isEdit ? t("automation.save") : t("automation.create");

    // Resolve the initially selected model to an *enabled* one. Disabled or
    // deleted models must never be offered / pre-selected in the dialog.
    const _dlgModels = getState().modelConfigs || [];
    const initModelIndex = (() => {
      const idx = editJob?.model_index ?? getState().activeModelIndex;
      if (_dlgModels[idx] && _dlgModels[idx].enabled !== false) return idx;
      const first = _dlgModels.findIndex((m) => m.enabled !== false);
      return first >= 0 ? first : idx;
    })();

    const overlay = document.createElement("div");
    overlay.className = "toast-overlay";
    overlay.innerHTML = `
      <div class="toast-dialog dialog-wide">
        <div class="toast-title">${title}</div>
        <div class="dialog-body">
            <div class="model-form-row">
              <label class="model-form-label">${t("automation.taskName")}</label>
              <input type="text" id="auto-dlg-name" class="model-form-input" placeholder="${t("automation.taskNamePlaceholder")}" value="${escapeHtml(editJob?.name || (template.defaultNameKey ? t(`automation.${template.defaultNameKey}`) : ""))}" />
            </div>
            <div class="model-form-row">
              <label class="model-form-label">${t("automation.triggerTime")}</label>
              <div class="model-form-dropdown-row auto-trigger-row" id="auto-dlg-trigger-row">
                <div class="settings-dropdown-wrap auto-trigger-sched" id="auto-dlg-schedule-wrap">
                  <button class="settings-dropdown-trigger" id="auto-dlg-schedule-trigger" type="button">
                    <i data-lucide="${parsed.scheduleType === "hourly" ? "clock" : "calendar"}" class="lucide auto-trigger-icon"></i>
                    <span>${parsed.scheduleType === "daily" ? t("automation.everyDay") : parsed.scheduleType === "weekly" ? t("automation.everyWeek") : t("automation.everyHour")}</span>
                    <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
                  </button>
                  <div class="settings-dropdown" id="auto-dlg-schedule-dropdown">
                    <div class="settings-dropdown-item" data-value="daily"><span>${t("automation.everyDay")}</span></div>
                    <div class="settings-dropdown-item" data-value="weekly"><span>${t("automation.everyWeek")}</span></div>
                    <div class="settings-dropdown-item" data-value="hourly"><span>${t("automation.everyHour")}</span></div>
                  </div>
                </div>
                <div class="settings-dropdown-wrap auto-trigger-time" id="auto-dlg-time-wrap">
                  <button class="settings-dropdown-trigger auto-time-trigger" id="auto-dlg-time-trigger" type="button">
                    <span class="auto-time-value">${escapeHtml(parsed.time)}</span>
                    <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
                  </button>
                  <div class="settings-dropdown time-picker-dropdown" id="auto-dlg-time-dropdown">
                    <div class="time-picker-grid">
                      <div class="time-picker-col" id="auto-dlg-time-hours"></div>
                      <div class="time-picker-col" id="auto-dlg-time-mins"></div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          <div class="model-form-row auto-model-perm-row">
            <div class="auto-model-perm-col">
              <label class="model-form-label">${t("automation.modelLabel")}</label>
              <div class="model-form-dropdown-row">
                <div class="settings-dropdown-wrap" id="auto-dlg-model-wrap">
                  <button class="settings-dropdown-trigger" id="auto-dlg-model-trigger" type="button">
                    <span>${(_dlgModels[initModelIndex]?.name) || "—"}</span>
                    <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
                  </button>
                  <div class="settings-dropdown" id="auto-dlg-model-dropdown"></div>
                </div>
              </div>
            </div>
            <div class="auto-model-perm-col">
              <label class="model-form-label">${t("input.permissionMode")}</label>
              <div class="model-form-dropdown-row">
                <div class="settings-dropdown-wrap" id="auto-dlg-perm-wrap">
                  <button class="settings-dropdown-trigger" id="auto-dlg-perm-trigger" type="button">
                    <span class="auto-perm-mode-icon" id="auto-dlg-perm-mode-icon"></span>
                    <span class="auto-perm-mode-label"></span>
                    <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
                  </button>
                  <div class="settings-dropdown" id="auto-dlg-perm-dropdown"></div>
                </div>
              </div>
            </div>
          </div>
          <div class="model-form-row">
            <label class="model-form-label">${t("automation.workspaceLabel")}</label>
            <div class="model-form-dropdown-row">
              <div class="settings-dropdown-wrap" id="auto-dlg-workspace-wrap">
                <button class="settings-dropdown-trigger" id="auto-dlg-workspace-trigger" type="button">
                  <span></span>
                  <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
                </button>
                <div class="settings-dropdown" id="auto-dlg-workspace-dropdown"></div>
              </div>
            </div>
          </div>
          <div class="settings-card model-selection-card" style="margin-bottom:12px;overflow:hidden">
            <div class="settings-item-row">
              <div class="settings-item-info">
                <div class="settings-item-title">
                  <span>${t("automation.pushCardTitle")}</span>
                </div>
                <div class="settings-item-desc">${t("automation.enablePushHint")}</div>
              </div>
              <div class="settings-item-control">
                <label class="toggle-switch" title="${t("automation.enablePush")}">
                  <input type="checkbox" id="auto-dlg-push-toggle" ${editJob?.push_gateways?.length ? "checked" : ""} />
                  <span class="toggle-slider"></span>
                </label>
              </div>
            </div>
            <div id="auto-dlg-push-gateways-row" class="model-selection-row${editJob?.push_gateways?.length ? " model-selection-row--open" : ""}">
              <div class="model-selection-row-inner">
                <div class="auto-push-gateways">
                  <div class="auto-push-gateways-label">${t("automation.pushGateways")}</div>
                  <div id="auto-dlg-push-gateways" class="model-selection-list"></div>
                </div>
              </div>
            </div>
          </div>
          <div class="model-form-row" style="flex-direction:column;align-items:stretch">
            <label class="model-form-label">${t("automation.whatToDo")}</label>
            <div class="input-wrapper input-wrapper--dialog">
              <textarea id="auto-dlg-prompt" class="setting-rich-text" placeholder="${t("automation.whatToDo")}" style="min-height:80px">${escapeHtml(editJob?.prompt || template.defaultPrompt)}</textarea>
            </div>
          </div>
        </div>
        <div class="dialog-footer">
          <button class="btn" id="auto-dlg-cancel">${t("dialog.cancel")}</button>
          <button class="btn btn--primary" id="auto-dlg-ok">${btnLabel}</button>
        </div>
      </div>`;

    // Dock inside the automation view: the overlay covers only this panel
    // (absolute positioning, see styles.css) and disappears with the view —
    // in-view isolation, same idea as tab-scoped dialogs in session_inner.
    this.el.appendChild(overlay);
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }
    attachInputExpand(overlay);

    // Auto-resize prompt textarea (same pattern as settings rule editor)
    const promptTa = overlay.querySelector("#auto-dlg-prompt") as HTMLTextAreaElement;
    if (promptTa) {
      let raf = 0;
      const resize = () => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          promptTa.style.height = "auto";
          promptTa.style.height = Math.min(promptTa.scrollHeight, 300) + "px";
        });
      };
      promptTa.addEventListener("input", resize);
      resize();
    }

    let scheduleValue = parsed.scheduleType;
    let timeValue = parsed.time;
    const triggerRow = overlay.querySelector("#auto-dlg-trigger-row") as HTMLElement;
    const timeWrap = overlay.querySelector("#auto-dlg-time-wrap") as HTMLElement;
    const timeTrigger = overlay.querySelector("#auto-dlg-time-trigger") as HTMLElement;
    const timeDropdown = overlay.querySelector("#auto-dlg-time-dropdown") as HTMLElement;
    const scheduleTrigger = overlay.querySelector("#auto-dlg-schedule-trigger") as HTMLElement;
    const scheduleDropdown = overlay.querySelector("#auto-dlg-schedule-dropdown") as HTMLElement;

    const pad2 = (n: number): string => n.toString().padStart(2, "0");

    /** Reflect the picked time into the trigger label. */
    const setTimeValue = (v: string) => {
      timeValue = v;
      timeTrigger.querySelector(".auto-time-value")!.textContent = v;
    };

    /** Scroll each column so its selected cell sits centred (no visible scrollbar). */
    const centerTimeSelection = () => {
      [ "#auto-dlg-time-hours", "#auto-dlg-time-mins" ].forEach((sel) => {
        const col = overlay.querySelector(sel) as HTMLElement | null;
        const cell = col?.querySelector(".settings-dropdown-item.selected") as HTMLElement | null;
        if (!col || !cell) return;
        col.scrollTop = Math.max(0, cell.offsetTop - (col.clientHeight - cell.offsetHeight) / 2);
      });
    };

    const buildTimeOptions = () => {
      const hoursCol = overlay.querySelector("#auto-dlg-time-hours") as HTMLElement;
      const minsCol = overlay.querySelector("#auto-dlg-time-mins") as HTMLElement;
      const [curH, curM] = (() => {
        const parts = (timeValue || "09:00").split(":").map(Number);
        return [parts[0] ?? 9, parts[1] ?? 0];
      })();
      const renderCol = (col: HTMLElement, count: number, selected: number, onPick: (v: number) => void) => {
        col.innerHTML = Array.from({ length: count }, (_, i) =>
          `<div class="settings-dropdown-item time-picker-cell${i === selected ? " selected" : ""}" data-value="${i}">${pad2(i)}</div>`
        ).join("");
        col.querySelectorAll(".settings-dropdown-item").forEach((item) => {
          item.addEventListener("click", (e) => {
            e.stopPropagation();
            onPick(parseInt((item as HTMLElement).getAttribute("data-value") || "0", 10));
          });
        });
      };
      renderCol(hoursCol, 24, curH, (h) => {
        const m = parseInt((timeValue || "09:00").split(":")[1] ?? "0", 10);
        setTimeValue(`${pad2(h)}:${pad2(m)}`);
        hoursCol.querySelectorAll(".settings-dropdown-item").forEach((item) => {
          item.classList.toggle("selected", parseInt(item.getAttribute("data-value") || "0", 10) === h);
        });
      });
      renderCol(minsCol, 60, curM, (m) => {
        const h = parseInt((timeValue || "09:00").split(":")[0] ?? "9", 10);
        setTimeValue(`${pad2(h)}:${pad2(m)}`);
        minsCol.querySelectorAll(".settings-dropdown-item").forEach((item) => {
          item.classList.toggle("selected", parseInt(item.getAttribute("data-value") || "0", 10) === m);
        });
      });
    };

    const showTimePicker = (show: boolean) => {
      // One row: the schedule control sits left, the time control right.
      // Hourly runs have no wall-clock time, so the row collapses to a single
      // full-width schedule control instead of leaving a hole.
      timeWrap.style.display = show ? "" : "none";
      triggerRow.classList.toggle("auto-trigger-row--single", !show);
    };

    scheduleTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = scheduleDropdown.classList.contains("open");
      if (isOpen) { scheduleDropdown.classList.remove("open"); return; }
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      scheduleDropdown.classList.add("open");
    });

    scheduleDropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
      item.addEventListener("click", (e) => {
        e.stopPropagation();
        const val = (item as HTMLElement).getAttribute("data-value") || "daily";
        const label = (item as HTMLElement).querySelector("span")!.textContent || "";
        scheduleTrigger.querySelector("span")!.textContent = label;
        const schedIcon = scheduleTrigger.querySelector("i[data-lucide]") as HTMLElement | null;
        if (schedIcon) {
          schedIcon.setAttribute("data-lucide", val === "hourly" ? "clock" : "calendar");
          if (typeof (window as any).lucide !== "undefined") {
            (window as any).lucide.createIcons({ root: scheduleTrigger });
          }
        }
        scheduleDropdown.classList.remove("open");
        scheduleValue = val;
        showTimePicker(val !== "hourly");
      });
    });

    const closeTimeOutside = (ev: MouseEvent) => {
      const target = ev.target as Node;
      if (timeDropdown.classList.contains("open") && !timeDropdown.contains(target) && !timeTrigger.contains(target)) {
        timeDropdown.classList.remove("open");
      }
    };
    timeTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = timeDropdown.classList.contains("open");
      if (isOpen) { timeDropdown.classList.remove("open"); return; }
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      buildTimeOptions();
      timeDropdown.classList.add("open");
      // Columns are display:none until `.open` lands — centre only afterwards.
      requestAnimationFrame(centerTimeSelection);
    });
    document.addEventListener("click", closeTimeOutside);

    let selectedModelIndex = initModelIndex;
    const modelTrigger = overlay.querySelector("#auto-dlg-model-trigger") as HTMLElement;
    const modelDropdown = overlay.querySelector("#auto-dlg-model-dropdown") as HTMLElement;

    /**
     * Display name of the provider owning a flat `modelConfigs` index — the
     * right-hand label of each dropdown row.  Mirrors the provider naming used
     * by the model picker: custom providers show their user-defined name, the
     * rest their catalog label.  "" when the provider snapshot is out of sync
     * (callers then fall back to the model id).
     */
    const providerNameFor = (flatIdx: number): string => {
      const loc = locateModelInProviders(flatIdx);
      const providers = getState().providers || [];
      const provider = loc ? providers[loc.providerIdx] : undefined;
      if (!provider) return "";
      const catalog = getState().modelCatalog;
      const label = catalog?.providers?.find((c) => c.id === provider.backend_type)?.label
        || provider.backend_type || "";
      return provider.backend_type === "openai_compatible"
        ? (provider.name?.trim() || label)
        : label;
    };

    const buildModelOptions = () => {
      const allModels = getState().modelConfigs;
      const models = allModels ? allModels.filter((m) => m.enabled !== false) : [];
      if (models.length === 0) {
        modelDropdown.innerHTML = `<div class="settings-dropdown-item" style="opacity:0.5;cursor:default">—</div>`;
        return;
      }
      modelDropdown.innerHTML = models.map((m) => {
        const origIdx = allModels.indexOf(m);
        // Right-hand label = provider, NOT the model id: `name` defaults to
        // `model_id`, so showing the id too read as the model name twice.
        const provider = providerNameFor(origIdx) || m.model_id;
        return `<div class="settings-dropdown-item${origIdx === selectedModelIndex ? " selected" : ""}" data-index="${origIdx}">
          <span>${escapeHtml(m.name)}</span>
          <span style="opacity:0.5;margin-inline-start:6px;font-size:11px">${escapeHtml(provider)}</span>
        </div>`;
      }).join("");
      modelDropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          const idx = parseInt((item as HTMLElement).getAttribute("data-index") || "-1");
          if (idx < 0) return;
          selectedModelIndex = idx;
          const models = getState().modelConfigs;
          modelTrigger.querySelector("span")!.textContent = models[idx]?.name || "—";
          modelDropdown.classList.remove("open");
        });
      });
    };

    modelTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = modelDropdown.classList.contains("open");
      if (isOpen) { modelDropdown.classList.remove("open"); return; }
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      buildModelOptions();
      modelDropdown.classList.add("open");
    });

    // ── Permission mode selector ──
    // The same permission system every other mode uses: the four preset
    // groups the safety engine dispatches on (manual / auto / full / custom).
    // The raw value rides the job's agent config and is applied to the
    // sub-agent config at schedule time, so approval really takes effect.
    const permTrigger = overlay.querySelector("#auto-dlg-perm-trigger") as HTMLElement;
    const permDropdown = overlay.querySelector("#auto-dlg-perm-dropdown") as HTMLElement;
    let selectedPermission = permissionModeGroup(editJob?.permission_mode || "default");
    // The collapsed trigger mirrors the mode list: current mode's icon + name,
    // repainted on every applied switch.
    const permLabelEl = permTrigger.querySelector(".auto-perm-mode-label") as HTMLElement;
    const permIconEl = permTrigger.querySelector(".auto-perm-mode-icon") as HTMLElement;
    const paintPermTrigger = () => {
      permLabelEl.textContent = permissionModeLabel(selectedPermission);
      permIconEl.innerHTML = `<i data-lucide="${permissionModeIcon(selectedPermission)}" class="lucide"></i>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: permIconEl });
      }
    };
    paintPermTrigger();

    const buildPermOptions = () => {
      // Same catalogue and icons as every other mode list (shared module).
      permDropdown.innerHTML = PERMISSION_MODES.map((m) =>
        `<div class="settings-dropdown-item${m.id === selectedPermission ? " selected" : ""}" data-perm-mode="${m.id}"><i data-lucide="${m.icon}" class="lucide"></i><span>${escapeHtml(t(m.labelKey))}</span></div>`
      ).join("");
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: permDropdown });
      }
      permDropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          void (async () => {
            // Shared guarded path: "bypass" requires the danger confirmation,
            // rejection keeps the previous selection.
            const applied = await guardPermissionMode(
              (item as HTMLElement).getAttribute("data-perm-mode") || "default",
              dialogPermissionConfirm,
            );
            permDropdown.classList.remove("open");
            if (applied === null) return;
            selectedPermission = applied;
            paintPermTrigger();
          })();
        });
      });
    };

    permTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = permDropdown.classList.contains("open");
      if (isOpen) { permDropdown.classList.remove("open"); return; }
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      buildPermOptions();
      permDropdown.classList.add("open");
    });

    // ── Workspace selector (optional execution context) ──
    // Empty selection keeps the backend's default execution sandbox; a picked
    // workspace is pinned into the job's agent config at schedule time.
    const wsTrigger = overlay.querySelector("#auto-dlg-workspace-trigger") as HTMLElement;
    const wsDropdown = overlay.querySelector("#auto-dlg-workspace-dropdown") as HTMLElement;
    let selectedWorkspace = editJob?.workspace || "";

    const wsLabelFor = (path: string): string => {
      if (!path) return t("automation.workspaceNone");
      const ws = (getState().workspaces || []).find((w) => w.path === path);
      return ws?.name || path;
    };
    wsTrigger.querySelector("span")!.textContent = wsLabelFor(selectedWorkspace);

    const buildWorkspaceOptions = () => {
      const list = (getState().workspaces || []).slice();
      // Keep a stale binding selectable so editing a job whose workspace was
      // later removed never silently rewrites it to "none".
      if (selectedWorkspace && !list.some((w) => w.path === selectedWorkspace)) {
        list.push({ id: "", path: selectedWorkspace, name: selectedWorkspace, opened_at: 0 });
      }
      if (list.length === 0) {
        wsDropdown.innerHTML = `<div class="settings-dropdown-item" style="opacity:0.5;cursor:default">${t("workspace.noWorkspaces")}</div>`;
        return;
      }
      wsDropdown.innerHTML = [
        `<div class="settings-dropdown-item${selectedWorkspace ? "" : " selected"}" data-ws-path="">${escapeHtml(t("automation.workspaceNone"))}</div>`,
        ...list.map((w) => `<div class="settings-dropdown-item${w.path === selectedWorkspace ? " selected" : ""}" data-ws-path="${escapeHtml(w.path)}"><span>${escapeHtml(w.name)}</span></div>`),
      ].join("");
      wsDropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          const path = (item as HTMLElement).getAttribute("data-ws-path") || "";
          selectedWorkspace = path;
          wsTrigger.querySelector("span")!.textContent = wsLabelFor(path);
          wsDropdown.classList.remove("open");
        });
      });
    };

    wsTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = wsDropdown.classList.contains("open");
      if (isOpen) { wsDropdown.classList.remove("open"); return; }
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      buildWorkspaceOptions();
      wsDropdown.classList.add("open");
    });

    if (parsed.scheduleType === "hourly") showTimePicker(false);

    // ── Push gateway toggle ──
    const pushToggle = overlay.querySelector("#auto-dlg-push-toggle") as HTMLInputElement;
    const pushGatewaysRow = overlay.querySelector("#auto-dlg-push-gateways-row") as HTMLElement;
    const pushGatewaysContainer = overlay.querySelector("#auto-dlg-push-gateways") as HTMLElement;

    const buildPushGateways = () => {
      const gs = getState().gatewayStatus;
      const adapters = gs?.adapters || [];
      const available = adapters.filter(a => a.connected).slice().sort((a, b) =>
        gatewayDisplayName(a.name).localeCompare(gatewayDisplayName(b.name), undefined, { sensitivity: "base" })
      );
      if (available.length === 0) {
        pushGatewaysContainer.innerHTML = `<span style="color:var(--text-muted);font-size:13px">${t("automation.pushGatewaysEmpty")}</span>`;
        return;
      }
      const selected = new Set<string>(editJob?.push_gateways || []);
      pushGatewaysContainer.innerHTML = available.map(a => {
        const isSel = selected.has(a.name);
        const iconHtml = platformIconHtml(a.name, 18);
        return `<div class="auto-push-gw-item${isSel ? " selected" : ""}" data-gateway-id="${a.name}">
          ${iconHtml}
          <span class="auto-push-gw-name">${escapeHtml(gatewayDisplayName(a.name))}</span>
          <span class="auto-push-gw-dot" title="${escapeHtml(a.name)}"></span>
          <span class="auto-push-gw-check"></span>
        </div>`;
      }).join("");
      pushGatewaysContainer.querySelectorAll(".auto-push-gw-item").forEach(item => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          const gwId = (item as HTMLElement).getAttribute("data-gateway-id");
          if (!gwId) return;
          const wasSel = selected.has(gwId);
          if (wasSel) {
            selected.delete(gwId);
            item.classList.remove("selected");
          } else {
            selected.add(gwId);
            item.classList.add("selected");
          }
        });
      });
    };

    pushToggle.addEventListener("change", () => {
      // `.model-selection-row--open` (grid-template-rows 0fr → 1fr) is the shared
      // expand motion — never flip `display` here, that is what made this row
      // snap while the gateway card's own pool glided.
      if (pushToggle.checked) buildPushGateways();
      pushGatewaysRow.classList.toggle("model-selection-row--open", pushToggle.checked);
    });

    // In edit mode with push enabled, populate gateways immediately
    if (pushToggle.checked) {
      buildPushGateways();
    }

    const close = () => removeAnimated(overlay);
    overlay.querySelector("#auto-dlg-cancel")?.addEventListener("click", close);

    overlay.querySelector("#auto-dlg-ok")?.addEventListener("click", () => {
      // Read inputs from THIS dialog only.  These ids are not unique when a
      // second create/edit dialog is opened before the first is removed; a
      // global getElementById would then read the first dialog's fields and
      // create a job from the wrong form.
      const name = (overlay.querySelector("#auto-dlg-name") as HTMLInputElement)?.value.trim();
      if (!name) return;

      const prompt = (overlay.querySelector("#auto-dlg-prompt") as HTMLTextAreaElement)?.value?.trim() || template.defaultPrompt;

      const time = timeValue || "09:00";
      const [h, m] = time.split(":").map(s => s.padStart(2, "0"));
      let cron: string;
      switch (scheduleValue) {
        case "daily": cron = `${m} ${h} * * *`; break;
        case "weekly": cron = `${m} ${h} * * 1`; break;
        default: cron = `${m} * * * *`; break;
      }

      // Read push gateway state before removing overlay (elements become detached)
      let selectedPushGateways: string[] = [];
      const pushToggleEl = overlay.querySelector("#auto-dlg-push-toggle") as HTMLInputElement | null;
      if (pushToggleEl?.checked) {
        const container = overlay.querySelector("#auto-dlg-push-gateways");
        if (container) {
          selectedPushGateways = Array.from(container.querySelectorAll(".auto-push-gw-item.selected"))
            .map(item => (item as HTMLElement).getAttribute("data-gateway-id")!)
            .filter(Boolean);
        }
      }

      removeAnimated(overlay);

      if (isEdit && editJob) {
        send({
          type: "automation_update_job",
          job_id: editJob.id,
          name,
          prompt,
          cron,
          tag: editJob.tag || "",
          model_index: selectedModelIndex,
          workspace: selectedWorkspace,
          permission_mode: selectedPermission,
          push_gateways: selectedPushGateways,
        } as any);
      } else {
        send({
          type: "automation_create_job",
          name,
          prompt,
          cron,
          tag: template.titleKey ? t(template.titleKey) : "",
          model_index: selectedModelIndex,
          workspace: selectedWorkspace,
          permission_mode: selectedPermission,
          push_gateways: selectedPushGateways,
        } as any);
      }
    });
  }

  private switchTab(tab: string): void {
    this.activeTab = tab;

    this.tabsEl.querySelectorAll(".automation-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-automation-tab") === tab);
    });

    this.panelsEl.querySelectorAll(".automation-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.getAttribute("data-automation-panel") === tab);
    });

    if (tab === "configured") {
      this.requestJobsList();
    } else if (tab === "history") {
      this.requestHistory();
    }
  }

  private bindCreateButton(): void {
    const renderDropdown = () => {
      const blankHtml = `<div class="settings-dropdown-item" data-template-id="">
        <i data-lucide="plus" class="lucide" style="width:16px;height:16px;margin-inline-end:6px"></i>
        <span>${t("automation.customCreate")}</span>
      </div>`;
      this.createDropdown.innerHTML = blankHtml + TEMPLATES.map(
        (tmpl) => `
        <div class="settings-dropdown-item" data-template-id="${tmpl.id}">
          <i data-lucide="${tmpl.icon}" class="lucide" style="width:16px;height:16px;margin-inline-end:6px"></i>
          <span>${t(tmpl.titleKey)}</span>
        </div>
      `).join("");

      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: this.createDropdown });
      }

      this.createDropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          this.createDropdown.classList.remove("open");
          const id = (item as HTMLElement).getAttribute("data-template-id");
          if (!id) {
            const blank: TaskTemplate = { id: "custom", icon: "plus", titleKey: "", descKey: "", defaultNameKey: "", defaultPrompt: "", defaultCron: "" };
            this.openDialog(blank);
          } else {
            const template = TEMPLATES.find((t) => t.id === id);
            if (template) this.openDialog(template);
          }
        });
      });
    };

    this.createBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = this.createDropdown.classList.contains("open");
      if (isOpen) {
        this.createDropdown.classList.remove("open");
      } else {
        renderDropdown();
        this.createDropdown.classList.add("open");
        // CSS anchors this dropdown with `inset-inline-end: 0`, so under rtl
        // it grows towards the window edge. Pull it back if it overflows.
        clampIntoViewport(this.createDropdown);
      }
    });

    document.addEventListener("click", (e) => {
      if (!this.createWrap.contains(e.target as Node)) {
        this.createDropdown.classList.remove("open");
      }
    });
  }

  /** Renders the list of configured jobs. */
  private renderConfigured(): void {
    const wrap = this.configuredListEl;

    if (this.jobs.length === 0) {
      wrap.innerHTML = `<div class="si-empty-center">
        <i data-lucide="calendar" class="lucide"></i>
        <span class="si-empty-title">${t("automation.noTasks")}</span>
      </div>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: wrap });
      }
      return;
    }

    const stateTag = (state: string, suspended: boolean): string => {
      if (suspended) return `<span class="model-active-tag" style="color:var(--text-muted);background:rgba(156,163,175,0.12)">${t("automation.statePaused")}</span>`;
      switch (state) {
        case "RUNNING": return `<span class="model-active-tag" style="color:var(--success);background:rgba(34,197,94,0.12)">${t("automation.stateRunning")}</span>`;
        case "COMPLETED": return `<span class="model-active-tag" style="color:var(--success);background:rgba(34,197,94,0.12)">${t("automation.stateSuccess")}</span>`;
        case "FAILED": return `<span class="model-active-tag" style="color:var(--danger);background:rgba(239,68,68,0.12)">${t("automation.stateFailed")}</span>`;
        case "PENDING": return `<span class="model-active-tag" style="color:var(--warning);background:rgba(234,179,8,0.12)">${t("automation.statePending")}</span>`;
        default: return `<span class="model-active-tag" style="color:var(--text-muted);background:rgba(156,163,175,0.12)">${escapeHtml(state)}</span>`;
      }
    };

    let rowsHtml = "";
    for (const job of this.jobs) {
      const running = !job.suspended && (job.state === "RUNNING" || job.state === "PENDING");
      rowsHtml += `
        <div class="model-table-row" data-configured-id="${escapeHtml(job.id)}">
          <div class="model-table-cell model-cell-name">
            <span class="model-name-text">${escapeHtml(job.name)}</span>
            ${job.tag && job.tag !== job.name ? `<span class="model-active-tag">${escapeHtml(job.tag)}</span>` : ""}
          </div>
          <div class="model-table-cell model-cell-provider">
            <div>${formatSchedule(job.cron)}</div>
          </div>
          <div class="model-table-cell model-cell-actions">
            ${stateTag(job.state, job.suspended)}
            <button class="btn-icon" data-action="edit" data-tooltip="${t("general.edit")}">
              <i data-lucide="pencil" class="lucide"></i>
            </button>
            <button class="btn-icon btn-icon--danger" data-action="delete" data-tooltip="${t("general.delete")}">
              <i data-lucide="trash-2" class="lucide"></i>
            </button>
            <label class="toggle-switch toggle-sm">
              <input type="checkbox" class="auto-toggle" data-job-id="${escapeHtml(job.id)}" ${running ? "checked" : ""} />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>`;
    }

    this.configuredListEl.innerHTML = `
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${t("automation.configuredDesc")}</div>
        </div>
        <div class="model-table">
          <div class="model-table-header">
            <div class="model-table-cell model-cell-name">${t("automation.taskName")}</div>
            <div class="model-table-cell model-cell-provider">${t("automation.triggerTime")}</div>
            <div class="model-table-cell model-cell-actions">${t("automation.configured")}</div>
          </div>
          ${rowsHtml}
        </div>
      </div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.configuredListEl });
    }

    this.configuredListEl.querySelectorAll(".model-table-row").forEach((row) => {
      const id = (row as HTMLElement).getAttribute("data-configured-id")!;
      const job = this.jobs.find(j => j.id === id);

      row.querySelector('[data-action="edit"]')?.addEventListener("click", (e) => {
        e.stopPropagation();
        if (job) this.onConfiguredEdit(job);
      });

      row.querySelector('[data-action="delete"]')?.addEventListener("click", (e) => {
        e.stopPropagation();
        if (job) this.onConfiguredAction(id, "delete");
      });
    });

    // Bind toggle switches — matches settings model toggle pattern
    this.configuredListEl.querySelectorAll(".auto-toggle").forEach((cb) => {
      cb.addEventListener("change", () => {
        const jobId = (cb as HTMLElement).getAttribute("data-job-id") || "";
        this.onConfiguredToggle(jobId);
      });
    });
  }

  /**
   * Keep the date range sane: the start date must never be after the end
   * date. When an out-of-order pick happens the counterpart field is moved
   * so the range stays valid (picking a start after the end moves the end;
   * picking an end before the start moves the start). Also refreshes the
   * visible date labels, hidden inputs and the clear button.
   */
  private clampDateRange(changed: "from" | "to"): void {
    const from = this.historyDateFrom;
    const to = this.historyDateTo;
    if (from && to && from > to) {
      if (changed === "from") {
        this.historyDateTo = from;
      } else {
        this.historyDateFrom = to;
      }
    }
    const fromHidden = document.getElementById("history-date-from") as HTMLInputElement | null;
    const toHidden = document.getElementById("history-date-to") as HTMLInputElement | null;
    const fromText = document.getElementById("history-date-from-text");
    const toText = document.getElementById("history-date-to-text");
    if (fromHidden) fromHidden.value = this.historyDateFrom;
    if (toHidden) toHidden.value = this.historyDateTo;
    if (fromText) fromText.textContent = this.historyDateFrom || t("automation.dateStart");
    if (toText) toText.textContent = this.historyDateTo || t("automation.dateEnd");
    this._syncDateClearVisibility?.();
    this.renderHistory();
  }

  private bindHistoryFilters(): void {
    if (this.historyFiltersBound) return;
    const root = document.getElementById("history-filters");
    if (!root) return;
    this.historyFiltersBound = true;

    // ── Status dropdown ──
    const rebindStatus = () => {
      const statusOptions: { id: string; label: string }[] = [
        { id: "", label: t("automation.filterAll") },
        { id: "COMPLETED", label: t("automation.filterSuccess") },
        { id: "FAILED", label: t("automation.filterFailed") },
        { id: "RUNNING", label: t("automation.stateRunning") },
        { id: "PENDING", label: t("automation.statePending") },
      ];
      this.renderHistoryDropdown("history-filter-status", statusOptions, this.historyStatus, (val) => {
        this.historyStatus = val;
        this.renderHistory();
      });
    };
    rebindStatus();
    this._rebindStatus = rebindStatus;

    // ── Task dropdown ──
    const renderTaskOptions = () => {
      const opts: { id: string; label: string }[] = [{ id: "", label: t("automation.filterAllTasks") }];
      const seenNames = new Set<string>();
      for (const h of getState().automationHistory) {
        const name = h.name || h.job_id || "";
        if (!name || seenNames.has(name)) continue;
        seenNames.add(name);
        opts.push({ id: h.job_id || "", label: name });
      }
      for (const j of this.jobs) {
        const name = j.name || j.id || "";
        if (!name || seenNames.has(name)) continue;
        seenNames.add(name);
        opts.push({ id: j.id, label: name });
      }
      return opts;
    };
    const rebindTask = () => {
      this.renderHistoryDropdown("history-filter-task", renderTaskOptions(), this.historyTaskId, (val) => {
        this.historyTaskId = val;
        this.renderHistory();
      });
    };
    rebindTask();
    this._rebindTask = rebindTask;
    // re-render task list when history/jobs change
    this.onHistoryFiltersRebind = rebindTask;

    // ── Date range (custom calendar dropdown) ──
    const dateFromHidden = document.getElementById("history-date-from") as HTMLInputElement | null;
    const dateToHidden = document.getElementById("history-date-to") as HTMLInputElement | null;
    const dateFromBtn = document.getElementById("history-date-from-btn");
    const dateToBtn = document.getElementById("history-date-to-btn");
    const dateFromDD = document.getElementById("history-date-from-dd");
    const dateToDD = document.getElementById("history-date-to-dd");
    const dateFromText = document.getElementById("history-date-from-text");
    const dateToText = document.getElementById("history-date-to-text");
    this._dateFromText = dateFromText;
    this._dateToText = dateToText;
    // Set locale-aware placeholder text (refreshed on language change via updateHistoryFilterLabels).
    if (dateFromText) dateFromText.textContent = t("automation.dateStart");
    if (dateToText) dateToText.textContent = t("automation.dateEnd");

    const MONTHS_SHORT = [
      t("automation.month1"), t("automation.month2"), t("automation.month3"), t("automation.month4"),
      t("automation.month5"), t("automation.month6"), t("automation.month7"), t("automation.month8"),
      t("automation.month9"), t("automation.month10"), t("automation.month11"), t("automation.month12"),
    ];

    const WEEKDAYS_SHORT = [
      t("automation.weekday1"), t("automation.weekday2"), t("automation.weekday3"), t("automation.weekday4"),
      t("automation.weekday5"), t("automation.weekday6"), t("automation.weekday7"),
    ];

    function renderCalendar(container: HTMLElement, currentDate: { year: number; month: number }, onSelect: (dateStr: string) => void): void {
      const { year, month } = currentDate;
      const firstDay = new Date(year, month, 1).getDay();
      const daysInMonth = new Date(year, month + 1, 0).getDate();
      const daysInPrev = new Date(year, month, 0).getDate();
      const today = new Date();
      const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;

      const prevMonth = () => {
        if (--currentDate.month < 0) { currentDate.month = 11; currentDate.year--; }
        renderCalendar(container, currentDate, onSelect);
      };
      const nextMonth = () => {
        if (++currentDate.month > 11) { currentDate.month = 0; currentDate.year++; }
        renderCalendar(container, currentDate, onSelect);
      };

      let html = `<div class="cal-header">
        <button type="button" class="cal-header-btn" id="cal-prev"><svg class="lucide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg></button>
        <span class="cal-title">${year} ${MONTHS_SHORT[month]}</span>
        <button type="button" class="cal-header-btn" id="cal-next"><svg class="lucide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg></button>
      </div>
      <div class="cal-weekdays">${WEEKDAYS_SHORT.map(d => `<span class="cal-weekday">${d}</span>`).join("")}</div>
      <div class="cal-grid">`;

      const startOffset = (firstDay === 0 ? 6 : firstDay - 1);
      for (let i = 0; i < startOffset; i++) {
        const d = daysInPrev - startOffset + i + 1;
        html += `<span class="cal-day other-month">${d}</span>`;
      }
      for (let d = 1; d <= daysInMonth; d++) {
        const dateStr = `${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
        const isToday = dateStr === todayStr;
        const cls = `cal-day${isToday ? " today" : ""}`;
        html += `<span class="${cls}" data-date="${dateStr}">${d}</span>`;
      }
      const remaining = (7 - (startOffset + daysInMonth) % 7) % 7;
      for (let d = 1; d <= remaining; d++) {
        html += `<span class="cal-day other-month">${d}</span>`;
      }
      html += `</div>`;
      container.innerHTML = html;

      if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: container });

      container.querySelector("#cal-prev")?.addEventListener("click", (e) => { e.stopPropagation(); prevMonth(); });
      container.querySelector("#cal-next")?.addEventListener("click", (e) => { e.stopPropagation(); nextMonth(); });
      container.querySelectorAll(".cal-day[data-date]").forEach(el => {
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          onSelect((el as HTMLElement).getAttribute("data-date") || "");
          container.classList.remove("open");
        });
      });
    }

    function bindDateField(
      btn: HTMLElement | null,
      dd: HTMLElement | null,
      textEl: HTMLElement | null,
      hidden: HTMLInputElement | null,
      setter: (val: string) => void,
    ): void {
      if (!btn || !dd || !textEl) return;
      const cur = { year: new Date().getFullYear(), month: new Date().getMonth() };
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const isOpen = dd.classList.contains("open");
        document.querySelectorAll(".history-calendar-dropdown.open").forEach(el => el.classList.remove("open"));
        if (!isOpen) {
          renderCalendar(dd, cur, (dateStr) => {
            if (hidden) hidden.value = dateStr;
            textEl.textContent = dateStr;
            setter(dateStr);
          });
          dd.classList.add("open");
        }
      });
    }

    bindDateField(dateFromBtn, dateFromDD, dateFromText, dateFromHidden, (v) => { this.historyDateFrom = v; this.clampDateRange("from"); });
    bindDateField(dateToBtn, dateToDD, dateToText, dateToHidden, (v) => { this.historyDateTo = v; this.clampDateRange("to"); });

    // Close calendar dropdowns when clicking outside
    document.addEventListener("click", () => {
      document.querySelectorAll(".history-calendar-dropdown.open").forEach(el => el.classList.remove("open"));
    });

    const dateClear = document.getElementById("history-date-clear");
    // The clear (×) button only makes sense once a date has actually been
    // picked — hide it until then so the empty filter row stays clean.
    const syncDateClearVisibility = () => {
      if (!dateClear) return;
      const hasDate = !!(this.historyDateFrom || this.historyDateTo);
      dateClear.classList.toggle("hidden", !hasDate);
    };
    this._syncDateClearVisibility = syncDateClearVisibility;
    syncDateClearVisibility();
    if (dateClear) {
      dateClear.setAttribute("title", t("automation.clearDate"));
      dateClear.addEventListener("click", () => {
        this.historyDateFrom = "";
        this.historyDateTo = "";
        if (dateFromHidden) dateFromHidden.value = "";
        if (dateToHidden) dateToHidden.value = "";
        if (dateFromText) dateFromText.textContent = t("automation.dateStart");
        if (dateToText) dateToText.textContent = t("automation.dateEnd");
        syncDateClearVisibility();
        this.renderHistory();
      });
    }

  }

  /** Refresh history filter labels/dropdowns for the current locale. */
  private updateHistoryFilterLabels(): void {
    this._rebindStatus?.();
    this._rebindTask?.();
    // Refresh date placeholders only when no explicit date is selected.
    if (this._dateFromText && !this.historyDateFrom) {
      this._dateFromText.textContent = t("automation.dateStart");
    }
    if (this._dateToText && !this.historyDateTo) {
      this._dateToText.textContent = t("automation.dateEnd");
    }
  }

  private exportHistory(): void {
    if (this.currentFilteredHistory.length === 0) {
      // No records to export — silent no-op (button is hidden in this case)
      return;
    }
    const lines: string[] = [];
    lines.push(`# ${t("automation.exportHistoryTitle")}`);
    lines.push("");
    const now = formatDateTime(Math.floor(Date.now() / 1000));
    lines.push(`*${now}*`);
    lines.push("");
    lines.push(`**${this.currentFilteredHistory.length}** ${t("automation.history")}`);
    lines.push("");

    // Build task → entries map
    const taskMap = new Map<string, HistoryEntry[]>();
    for (const h of this.currentFilteredHistory) {
      const key = h.name || h.job_id;
      if (!taskMap.has(key)) taskMap.set(key, []);
      taskMap.get(key)!.push(h);
    }

    for (const [taskName, entries] of taskMap) {
      lines.push(`## ${taskName}`);
      lines.push("");
      // sort newest first
      const sorted = [...entries].sort((a, b) => b.time - a.time);
      for (const e of sorted) {
        const ts = formatDateTime(e.time);
        const stateLabel = this.getStateLabel(e.state);
        const stateBadge = e.state === "SUCCESS" || e.state === "COMPLETED" ? "✅"
          : e.state === "FAILED" ? "❌"
          : e.state === "RUNNING" ? "🔄"
          : "⏳";
        lines.push(`### ${stateBadge} ${ts} — ${stateLabel}`);
        if (e.tag && e.tag !== e.name) {
          lines.push("");
          lines.push(`- **${t("automation.modelLabel")}**: ${e.tag}`);
        }
        if (e.fail_count && e.fail_count > 0) {
          lines.push(`- **${t("automation.failedCount")}**: ${e.fail_count}`);
        }
        if (e.last_result) {
          lines.push("");
          lines.push("> " + e.last_result.replace(/\n/g, "\n> "));
        }
        if ((e as any).messages && Array.isArray((e as any).messages) && (e as any).messages.length > 0) {
          lines.push("");
          for (const m of (e as any).messages) {
            if (m.role === "user" || m.role === "human") {
              lines.push("**User:**");
              lines.push("");
              lines.push("```");
              lines.push(this.stripControlChars(String(m.content || "")));
              lines.push("```");
            } else if (m.role === "assistant" || m.role === "ai") {
              lines.push("**Assistant:**");
              lines.push("");
              lines.push(this.stripControlChars(String(m.content || "")));
            } else {
              lines.push(`**${m.role}:**`);
              lines.push("");
              lines.push("```");
              lines.push(this.stripControlChars(String(m.content || "")));
              lines.push("```");
            }
            lines.push("");
          }
        }
        lines.push("");
        lines.push("---");
        lines.push("");
      }
    }

    const md = lines.join("\n");
    const stamp = new Date();
    const pad = (n: number) => String(n).padStart(2, "0");
    const filename = `automation-history-${stamp.getFullYear()}${pad(stamp.getMonth() + 1)}${pad(stamp.getDate())}-${pad(stamp.getHours())}${pad(stamp.getMinutes())}.md`;
    downloadMarkdownFile(md, filename);
  }

  private stripControlChars(s: string): string {
    return s.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
  }

  private getStateLabel(state: string): string {
    switch (state) {
      case "SUCCESS":
      case "COMPLETED": return t("automation.stateSuccess");
      case "FAILED": return t("automation.stateFailed");
      case "RUNNING": return t("automation.stateRunning");
      case "PENDING": return t("automation.statePending");
      case "PAUSED": return t("automation.statePaused");
      default: return state;
    }
  }

  private onHistoryFiltersRebind?: () => void;

  private renderHistoryDropdown(
    id: string,
    options: { id: string; label: string }[],
    currentId: string,
    onChange: (val: string) => void,
  ): void {
    const trigger = document.getElementById(`${id}-trigger`);
    let labelEl = document.getElementById(`${id}-label`);
    const dropdown = document.getElementById(`${id}-dropdown`);
    if (!trigger || !dropdown || !labelEl) return;

    const current = options.find((o) => o.id === currentId) || options[0];
    labelEl.textContent = current.label;

    // Rebuild dropdown content
    dropdown.innerHTML = options.map((o) => `
      <div class="settings-dropdown-item${o.id === currentId ? " selected" : ""}" data-value="${escapeHtml(o.id)}">${escapeHtml(o.label)}</div>
    `).join("");

    // Re-bind trigger click (replace node to clear old listeners). The clone
    // copies the child <span id="…-label">, so the previously captured
    // `labelEl` becomes detached — re-query it after the swap so label
    // updates land on the node that is actually in the DOM.
    const freshTrigger = trigger.cloneNode(true) as HTMLElement;
    trigger.parentNode!.replaceChild(freshTrigger, trigger);
    labelEl = document.getElementById(`${id}-label`);

    freshTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = dropdown.classList.contains("open");
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      if (!isOpen) dropdown.classList.add("open");
    });

    // Bind dropdown item clicks (delegated so it survives re-renders)
    dropdown.onclick = (e) => {
      const target = e.target as HTMLElement;
      const item = target.closest(".settings-dropdown-item") as HTMLElement | null;
      if (!item) return;
      e.stopPropagation();
      const val = item.getAttribute("data-value") || "";
      const label = item.textContent || "";
      if (labelEl) labelEl.textContent = label;
      dropdown.classList.remove("open");
      dropdown.querySelectorAll(".settings-dropdown-item").forEach((el) => el.classList.remove("selected"));
      item.classList.add("selected");
      onChange(val);
    };
  }

  /** Renders the run-history timeline (with active filters applied). */
  private renderHistory(): void {
    const filtersEl = document.getElementById("history-filters")!;

    // Only show actual execution history -- do NOT synthesize pending
    // entries for configured-but-not-yet-run jobs. A job that hasn't
    // executed has no history record and should not appear here.
    let displayHistory = [...getState().automationHistory];

    // ── Apply filters ──
    // Defensive: never let a reversed range silently hide everything. If the
    // two dates are out of order, treat them as their sorted bounds.
    let fromDate = this.historyDateFrom;
    let toDate = this.historyDateTo;
    if (fromDate && toDate && fromDate > toDate) {
      [fromDate, toDate] = [toDate, fromDate];
    }
    const fromTs = fromDate ? new Date(fromDate + "T00:00:00").getTime() / 1000 : null;
    const toTs = toDate ? new Date(toDate + "T23:59:59").getTime() / 1000 : null;
    displayHistory = displayHistory.filter((h) => {
      if (this.historyStatus && h.state !== this.historyStatus) return false;
      if (this.historyTaskId && h.job_id !== this.historyTaskId) return false;
      if (fromTs !== null && h.time < fromTs) return false;
      if (toTs !== null && h.time > toTs) return false;
      return true;
    });

    // Cache for export
    this.currentFilteredHistory = displayHistory;

    if (displayHistory.length === 0 && getState().automationHistory.length === 0) {
      filtersEl.style.display = "none";
      this.historyTimelineEl.innerHTML = `<div class="si-empty-center">
        <i data-lucide="history" class="lucide"></i>
        <span class="si-empty-title">${t("automation.noHistory")}</span>
      </div>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: this.historyTimelineEl });
      }
      return;
    }
    filtersEl.style.display = "";

    if (displayHistory.length === 0) {
      this.historyTimelineEl.innerHTML = `<div class="si-empty-center">
        <i data-lucide="search-x" class="lucide"></i>
        <span class="si-empty-title">${t("automation.noMatchingHistory")}</span>
      </div>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: this.historyTimelineEl });
      }
      return;
    }

    const groups: Record<string, HistoryEntry[]> = {};
    for (const h of displayHistory) {
      const dateKey = formatDate(h.time);
      if (!groups[dateKey]) groups[dateKey] = [];
      groups[dateKey].push(h);
    }

      this.historyTimelineEl.innerHTML = Object.entries(groups).map(
      ([date, records]) => `
      <div class="history-date-group">
        <div class="history-date-title">${date}</div>
        ${records.map((record) => {
          const state = record.state;
          const isRunning = state === "RUNNING";
          const isError = state === "FAILED";
          const iconName = isRunning ? "loader" : isError ? "x" : (state === "PENDING" ? "clock" : "check");
          const iconCls = isRunning ? "running" : isError ? "error" : (state === "PENDING" ? "pending" : "success");
          const metaText = `${formatDateTime(record.time)}`;
          return `
          <div class="history-timeline-item" data-entry-id="${escapeHtml(record.id)}"${record.session_id ? ` data-sid="${escapeHtml(record.session_id)}"` : ""}>
            <div class="history-timeline-icon history-timeline-icon--${iconCls}">
              <i data-lucide="${iconName}" class="lucide"></i>
            </div>
            <div class="history-timeline-content">
              <div class="history-timeline-header">
                <span class="history-timeline-name">${escapeHtml(record.name)}</span>
              </div>
              <div class="history-timeline-meta">
                ${metaText}
                ${record.fail_count > 0 ? `(${t("automation.failedCount")}: ${record.fail_count})` : ""}
              </div>
            </div>
          </div>
        `}).join("")}
      </div>
    `).join("");

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.historyTimelineEl });
    }

    // Click / right-click on a history row are DELEGATED on the timeline
    // container (bound once in the constructor) rather than attached per item:
    // `renderHistory()` rewrites the container's innerHTML on every history
    // broadcast, so per-item listeners had to be re-attached on each pass and
    // any row that survived a partial re-render (or a render that was
    // interrupted) silently lost its handlers — which is exactly what made
    // right-click appear dead.  Delegation cannot be lost by a re-render.
  }

  private onHistoryItemClick(item: HTMLElement): void {
    const entryId = item.getAttribute("data-entry-id");
    const entry = getState().automationHistory.find(h => h.id === entryId);
    if (!entry) return;
    const job = this.jobs.find(j => j.id === entry.job_id);
    const data: AutomationViewResult = {
      id: entry.id,
      name: entry.name,
      prompt: (entry as any).prompt || job?.prompt || entry.name,
      result: entry.last_result || "",
      tag: entry.tag,
      messages: entry.messages || [],
      state: entry.state,
      job_id: entry.job_id,
      session_id: entry.session_id,
    };
    this.openExecution(data);
  }

  private onHistoryItemContextMenu(item: HTMLElement, x: number, y: number): void {
    const sid = item.getAttribute("data-sid");
    const entryId = item.getAttribute("data-entry-id");
    const entry = getState().automationHistory.find(h => h.id === entryId);
    const isFailed = !!entry && entry.state === "FAILED";
    const doRename = () => {
      if (!entry) return;
      showRenameDialog(entry.name || "", (newName) => {
        if (entryId) {
          send({ type: "automation_rename_execution", entry_id: entryId, new_name: newName });
        }
      });
    };
    const doDelete = () => {
      if (!entryId) return;
      Dialog.confirm(t("automation.confirmDeleteRecord") || "Delete this record?", "", { scope: "app" }).then((confirmed) => {
        if (confirmed) {
          // Optimistically remove the entry from local state so the UI
          // updates instantly, matching the session delete pattern.
          const history = getState().automationHistory.filter((h: any) => h.id !== entryId);
          setAutomationHistory(history);
          send({ type: "automation_delete_execution", entry_id: entryId });
        }
      });
    };
    if (sid && !isFailed) {
      // Completed entries with a real sub-agent session: keep export
      // and session delete, but map rename to the execution record.
      // Archiving is meaningless for automation history entries.
      showSessionContextMenu(sid, x, y, true, false, undefined, doRename, true, item);
    } else if (sid && isFailed) {
      // Failed entries: reuse session context menu but hide export
      // (no session data to export) and override delete/remove to the
      // execution record. Rename also targets the execution record.
      // Archiving is meaningless for automation history entries.
      showSessionContextMenu(sid, x, y, true, true, doDelete, doRename, true, item);
    } else if (entryId) {
      // No sub-agent session — only rename/delete the execution record.
      this.showAutomationHistoryContextMenu(x, y, doRename, doDelete, item);
    }
  }

  private showAutomationHistoryContextMenu(
    x: number,
    y: number,
    doRename: () => void,
    doDelete: () => void,
    target?: HTMLElement | null,
  ): void {
    const menuEl = document.getElementById("session-context-menu")!;
    renderContextMenuItems(
      [
        { id: "ctx-automation-rename", icon: "pencil", label: this.escapeHtml(t("session.rename")), onClick: doRename },
        ...pluginMenuSpecs("automation_history"),
        { divider: true },
        { id: "ctx-automation-delete", icon: "trash-2", danger: true, label: this.escapeHtml(t("session.delete")), onClick: doDelete },
      ],
      { container: menuEl },
    );
    showContextMenu(menuEl, x, y, { target });
  }

  private onConfiguredAction(id: string, action: string): void {
    if (action === "delete") {
      const job = this.jobs.find((j) => j.id === id);
      const name = job?.name || id;
      Dialog.confirm(
        t("automation.confirmDeleteTaskTitle") || "Delete Task",
        t("automation.confirmDeleteTask", { name }) || `Delete task "${name}"? This cannot be undone.`,
        { scope: "app" },
      ).then((confirmed) => {
        if (!confirmed) return;
        send({ type: "automation_delete_job", job_id: id });
        // Refresh list immediately instead of relying on event chain
        this.requestJobsList();
        this.requestHistory();
      });
    }
  }

  private onConfiguredToggle(id: string): void {
    const job = this.jobs.find((j) => j.id === id);
    if (!job) return;
    send({ type: "automation_toggle_job", job_id: id });
  }

  private onConfiguredEdit(job: BackendJob): void {
    // Build a fake template to reuse openDialog in edit mode
    const fakeTemplate: TaskTemplate = {
      id: "edit",
      icon: "edit",
      titleKey: "automation.editTask",
      descKey: "",
      defaultNameKey: "",
      defaultPrompt: job.prompt,
      defaultCron: job.cron,
    };
    this.openDialog(fakeTemplate, job);
  }
}

function escapeHtml(str: string): string {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
