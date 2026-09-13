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
 * Per-item HTML renderers for the Chat transcript.
 *
 * Each renderer was extracted from the `Chat` class and is invoked with
 * `impl.call(this, ...)` from a thin class delegate, so `this` still refers
 * to the owning `Chat` instance and every private member stays reachable.
 */

// @ts-nocheck -- renderers relocated from the Chat class; called via
// `XImpl.call(this, ...)` so the instance context is preserved.

import type { Chat } from "./chat.js";
import type { ToolCallState } from "../core/types.js";
import type { TimelineItem } from "./timeline.js";
import { getState } from "../core/state.js";
import { t } from "../features/i18n.js";
import { findSlashCommand } from "../features/slash_commands.js";
import { renderMarkdown, escapeHtml } from "./chat_markdown.js";
import { isHiddenTool, isFileMutationTool, isFileReadTool, isToolItemTool, getToolIcon, formatToolName, getToolSummary, getToolInlineSummary, getToolBodyText, _toolStatusHtml, getAgentName, formatAgentLabel } from "./chat_tool_display.js";
import { _fmtTokens, fmtSize } from "./timeline.js";

export function renderItemHTMLImpl(this: Chat, item: TimelineItem): string {
    switch (item.kind) {
      case "user": return this.renderUserItem(item);
      case "ai_header": return this.renderAIHeader(item);
      case "thinking": return this.renderThinkingStrip(item);
      case "tool": return this.renderToolCall(item);
      case "assistant_text": return this.renderAssistantText(item);
      case "error_card": return this.renderErrorCard(item);
      case "warning_card": return this.renderWarningCard(item);
      case "compact_starting": return this.renderCompactStartingCard(item);
      case "compact": return this.renderCompactCard(item);
      case "system_message": return this.renderSystemMessage(item);
      case "spec_card": return this.renderSpecCard(item);
      case "plan_card": return this.renderPlanCard(item);
      case "workflow": return this.renderWorkflowCard(item);
    }
    return "";
  }

  export function renderAIHeaderImpl(this: Chat, item: TimelineItem): string {
    return `<div class="ai-header">
      <span class="ai-name">${t("chat.yimAgent")}</span>
      <span class="ai-time">${item.time}</span>
    </div>`;
  }

  export function renderModeCardImpl(this: Chat, icon: string, label: string, summary?: string): string {
    const iconHtml = icon.startsWith("data:")
      ? `<img src="${icon}" class="mode-card-icon" style="width:16px;height:16px">`
      : `<i data-lucide="${icon}" class="lucide mode-card-icon"></i>`;
    return `<span class="mode-chip mode-card">${iconHtml}<span class="mode-card-label">${escapeHtml(label)}</span>${summary ? `<span class="mode-card-summary">· ${escapeHtml(summary)}</span>` : ""}</span>`;
  }

  export function renderUserItemImpl(this: Chat, item: TimelineItem): string {
    const isSubAgent = !!getState().subAgentView || this._subAgentRender;
    const cmdMatch = item.content.match(/^\/(\w[\w-]*)(?:\s+(.*))?$/s);
    const cmdTagMatch = !cmdMatch ? item.content.match(/^<command>(\w[\w-]*)<\/command>$/s) : null;
    const effectiveCmdMatch = cmdMatch || cmdTagMatch;
    const isTerminal = item.mode?.startsWith("terminal:");
    const modeBadge = item.mode && !isTerminal ? (() => { const c = findSlashCommand(item.mode); const icon = c ? c.icon : "list-checks"; return `<span class="mode-chip" data-mode="${item.mode}"><i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i><span>${c ? c.title : item.mode}</span></span>`; })() : "";
    const fileCards = item.fileRefs?.map(f => {
      if (f.icon === "folder") return this.renderModeCard(f.icon, f.name, "folder");
      if (f.icon === "terminal") return this.renderModeCard(f.icon, f.name, `${f.size} line${f.size !== 1 ? "s" : ""}`);
      return this.renderModeCard(f.icon, f.name, fmtSize(f.size));
    }).join("") || "";
    const termCard = isTerminal ? this.renderModeCard("terminal", item.mode!.split(":")[1] || "Terminal", `${item.content.split("\n").length} lines`) : "";
    if (effectiveCmdMatch) {
      const cmdName = effectiveCmdMatch[1];
      const rest = cmdTagMatch ? "" : (effectiveCmdMatch[2] || "");
      const displayContent = rest ? escapeHtml(rest) : "";
      const cmd = findSlashCommand(cmdName);
      const cmdBadge = cmd
        ? `<span class="mode-chip" data-mode="${cmdName}"><i data-lucide="${cmd.icon}" class="chip-icon" style="width:12px;height:12px;"></i><span>${cmd.title}</span></span>`
        : `<span class="mode-chip" data-mode="${cmdName}"><i data-lucide="wand-2" class="chip-icon" style="width:12px;height:12px;"></i><span>${escapeHtml(cmdName)}</span></span>`;
      return `<div class="user-item" data-user-idx="${item.index}">
        <div class="user-bubble">${cmdBadge}${modeBadge}${displayContent}</div>
        <div class="user-actions">
          <button class="btn-icon btn-icon--msg msg-copy-btn" data-tooltip="${t("chat.copy")}">
            <i data-lucide="copy" class="lucide lucide-sm"></i>
          </button>
          ${isSubAgent ? "" : `<button class="btn-icon btn-icon--msg msg-rollback-btn" data-tooltip="${t("chat.rollbackEdit")}">
            <i data-lucide="history" class="lucide lucide-sm"></i>
          </button>
          <button class="btn-icon btn-icon--msg msg-delete-btn btn-icon--danger" data-tooltip="${t("chat.delete")}">
            <i data-lucide="trash-2" class="lucide lucide-sm"></i>
          </button>`}
        </div>
      </div>`;
    }
    const contentHtml = isSubAgent
      ? escapeHtml(item.content)
      : (item.content.includes("<attach ") || item.content.includes("<terminal>") || item.content.includes("<mode>")) ? "" : escapeHtml(item.content);
    return `<div class="user-item" data-user-idx="${item.index}">
      <div class="user-bubble">${modeBadge}${termCard}${fileCards}${contentHtml}</div>
      <div class="user-actions">
        <button class="btn-icon btn-icon--msg msg-copy-btn" data-tooltip="${t("chat.copy")}">
          <i data-lucide="copy" class="lucide lucide-sm"></i>
        </button>
        ${isSubAgent ? "" : `<button class="btn-icon btn-icon--msg msg-rollback-btn" data-tooltip="${t("chat.rollbackEdit")}">
          <i data-lucide="history" class="lucide lucide-sm"></i>
        </button>
        <button class="btn-icon btn-icon--msg msg-delete-btn btn-icon--danger" data-tooltip="${t("chat.delete")}">
          <i data-lucide="trash-2" class="lucide lucide-sm"></i>
        </button>`}
      </div>
    </div>`;
  }

  export function renderThinkingStripImpl(this: Chat, item: TimelineItem): string {
    const id = item.id;
    const expanded = this.expandedItems.has(id);
    return `<div class="strip-item${expanded ? " expanded" : ""}" data-id="${id}">
      <div class="strip" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        <span class="icon-wrap">
          <i data-lucide="brain" class="semantic"></i>
          <i data-lucide="chevron-down" class="arrow"></i>
        </span>
        <span class="strip-name">${t("chat.thought")}</span>
      </div>
      <div class="strip-body thought-body">${escapeHtml(item.text)}</div>
    </div>`;
  }

  export function renderToolCallImpl(this: Chat, item: TimelineItem): string {
    const tc = item.tc;
    const name = tc.name;
    if (isHiddenTool(name)) return "";
    if (name === "agent") return this.renderAgent(tc, item.id);
    if (name === "question") return this.renderQuestionCard(tc, item.id);
    if (name === "info") return this.renderInfoCard(tc, item.id);
    if (isFileMutationTool(name)) {
      // edit / write / file_write / file_edit / apply_patch render as a
      // collapsible strip whose body shows a GitHub-style diff — same
      // primitive as web_search. delete_file keeps the compact file-card.
      if (name === "delete_file") return this.renderFileCard(tc);
      return this.renderExpandableStrip(tc, item.id);
    }
    if (isFileReadTool(name)) return this.renderToolItemSimple(tc);
    if (isToolItemTool(name)) return this.renderToolItemSimple(tc);
    return this.renderExpandableStrip(tc, item.id);
  }

  export function renderInfoCardImpl(this: Chat, tc: ToolCallState, itemId: string): string {
    // Parse the info tool result. The backend returns a JSON payload with
    // display/title/content/media; fall back to treating the raw result as HTML.
    let payload: { display?: string; title?: string; content?: string; is_complete_html?: boolean; media?: Array<{type: string; src: string}> } = {};
    if (tc.result) {
      try {
        const parsed = JSON.parse(tc.result);
        if (parsed && typeof parsed === "object") payload = parsed;
      } catch {
        payload = { content: tc.result };
      }
    }

    const display = payload.display || (tc.params.display as string) || "base";
    const title = payload.title || (tc.params.title as string) || "";
    const content = payload.content || (tc.params.content as string) || "";
    const media = payload.media || [];
    const cardId = `tc-${tc.id}`;

    // Media card: render images/videos natively using MediaViewer.
    if (media.length > 0) {
      const itemsHtml = media.map((m, i) =>
        `<div data-type="${escapeHtml(m.type)}" data-src="${escapeHtml(m.src)}"></div>`
      ).join("");
      return `<div class="info-card info-card--media" id="${cardId}">${itemsHtml}</div>`;
    }

    if (!content) {
      return `<div class="info-card info-card--empty" id="${cardId}">
        <div class="info-card-header">
          <i data-lucide="layout-dashboard" class="info-card-icon"></i>
          <span class="info-card-title">${t("chat.toolInfo")}</span>
        </div>
        <div class="info-card-body">${t("chat.infoWaiting")}</div>
      </div>`;
    }

    // display='base' (default): strip card opens in child-window browser.
    const label = escapeHtml(title || t("chat.toolInfo"));
    return `<div class="strip-item" id="${cardId}" data-source="${escapeHtml(content)}">
      <div class="strip" onclick="window.__openInfoHtmlCard(this, '${cardId}')" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        <span class="icon-wrap">
          <i data-lucide="layout-dashboard" class="semantic"></i>
          <i data-lucide="chevron-right" class="arrow"></i>
        </span>
        <span class="strip-name">${label}</span>
      </div>
    </div>`;
  }

  export function renderFileCardImpl(this: Chat, tc: ToolCallState): string {
    // Invoked only for delete_file (see renderToolCall routing). Kept as a
    // compact one-line card; no diff badge (delete has no diff) and no
    // "View Changes" button (the file is gone, nothing to review).
    const id = `tc-${tc.id}`;
    const path = (tc.params.path as string) || (tc.params.file_path as string) || "";
    const hasResult = !!tc.result;
    const isError = hasResult && (tc.isError || /^error:/i.test(tc.result || ""));
    const iconName = isError ? "alert-triangle" : "trash-2";
    const iconColor = isError ? "#f59e0b" : "#71717a";
    const errorText = isError ? t("chat.toolFailed") : "";
    return `<div class="file-card${isError ? " file-card--error" : ""}" data-id="${id}" data-path="${escapeHtml(path)}">
      <i data-lucide="${iconName}" class="file-card-icon" style="color:${iconColor}"></i>
      <span class="file-card-name">${escapeHtml(path || formatToolName(tc.name))}</span>
      ${errorText ? `<span class="file-card-error-text">${errorText}</span>` : ""}
    </div>`;
  }

  export function renderTerminalCardImpl(this: Chat, tc: ToolCallState, itemId: string): string {
    const id = `tc-${tc.id}`;
    const expanded = this.expandedItems.has(id);
    const term = tc.params.terminal as string | undefined;
    const termIcon = "terminal";
    const summary = getToolSummary(tc);
    const hasResult = !!tc.result;
    const bodyHtml = hasResult ? getToolBodyText(tc) : "";
    return `<div class="terminal-card${expanded ? " expanded" : ""}" data-id="${id}">
      <div class="terminal-card-header" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        <span class="icon-wrap">
          <i data-lucide="${termIcon}" class="semantic"></i>
          <i data-lucide="chevron-down" class="arrow"></i>
        </span>
        <span class="strip-name">${formatToolName(tc.name, term)}</span>
        <span class="strip-summary">${summary ? `<code>${escapeHtml(summary)}</code>` : ""}</span>
      </div>
      <div class="terminal-body-slot" data-has-body="${hasResult ? "1" : "0"}">${bodyHtml ? `<div class="terminal-body">${bodyHtml}</div>` : ""}</div>
    </div>`;
  }

  export function renderExpandableStripImpl(this: Chat, tc: ToolCallState, itemId: string): string {
    const id = `tc-${tc.id}`;
    const expanded = this.expandedItems.has(id);
    const term = tc.params.terminal as string | undefined;
    const icon = getToolIcon(tc.name, term);
    const name = formatToolName(tc.name, term);
    const summary = getToolSummary(tc);
    const statusHtml = _toolStatusHtml(tc.status);
    const hasResult = !!tc.result;
    const bodyHtml = hasResult ? getToolBodyText(tc) : "";
    // Wrap the tool icon in an icon-wrap that swaps the semantic icon for
    // a chevron-down on hover, mirroring the thinking strip so the expand
    // affordance lives on the icon, not as a separate arrow on the right.
    const iconHtml = icon
      ? `<span class="icon-wrap"><i data-lucide="${icon}" class="semantic"></i><i data-lucide="chevron-down" class="arrow"></i></span>`
      : "";
    return `<div class="strip-item${expanded ? " expanded" : ""}" data-id="${id}">
      <div class="strip" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        ${iconHtml}
        <span class="strip-name">${name}</span>
        <span class="strip-status" data-status="${tc.status}">${statusHtml}</span>
        <span class="strip-summary">${summary ? escapeHtml(summary) : ""}</span>
      </div>
      <div class="strip-body-slot" data-has-body="${hasResult ? "1" : "0"}">${bodyHtml ? `<div class="strip-body">${bodyHtml}</div>` : ""}</div>
    </div>`;
  }

  export function renderToolItemSimpleImpl(this: Chat, tc: ToolCallState): string {
    const id = `tc-${tc.id}`;
    const term = tc.params.terminal as string | undefined;
    const icon = getToolIcon(tc.name, term);
    const name = formatToolName(tc.name, term);
    const summary = getToolInlineSummary(tc);
    const statusHtml = _toolStatusHtml(tc.status);
    const iconHtml = icon ? `<i data-lucide="${icon}" class="tool-item-icon"></i>` : "";
    return `<div class="tool-item" data-id="${id}">
      ${iconHtml}
      <span class="strip-name">${name}</span>
      <span class="tool-item-status" data-status="${tc.status}">${statusHtml}</span>
      <span class="strip-summary">${summary ? escapeHtml(summary) : ""}</span>
    </div>`;
  }

  export function renderAgentImpl(this: Chat, tc: ToolCallState, itemId: string): string {
    const rawAgentName = getAgentName(tc);
    const agentName = formatAgentLabel(rawAgentName);
    const isPlanSpec = tc.params.mode === "plan" || tc.params.mode === "spec";
    const agentIcon = isPlanSpec ? "list-checks" : "sparkles";
    const id = `tc-${tc.id}`;

    const tasks = this._agentTaskDividers(tc);
    if (tasks.length > 1) {
      return tasks.map((task, idx) => {
        const taskName = task.name || `${agentName} ${idx + 1}`;
        return `<div class="strip-item" id="${id}" data-task-index="${task.index}">
          <div class="strip" onclick="window.__openSubAgentView('${tc.id}', ${task.index})" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
            <span class="icon-wrap">
              <i data-lucide="${agentIcon}" class="semantic"></i>
              <i data-lucide="chevron-right" class="arrow"></i>
            </span>
            <span class="strip-name">${escapeHtml(taskName)}</span>
          </div>
        </div>`;
      }).join("");
    }

    return `<div class="strip-item" id="${id}">
      <div class="strip" onclick="window.__openSubAgentView('${tc.id}')" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        <span class="icon-wrap">
          <i data-lucide="${agentIcon}" class="semantic"></i>
          <i data-lucide="chevron-right" class="arrow"></i>
        </span>
        <span class="strip-name">${escapeHtml(agentName)}</span>
      </div>
    </div>`;
  }

  export function renderQuestionCardImpl(this: Chat, tc: ToolCallState, itemId: string): string {
    // Try to extract params — may be raw string or parsed object
    let params = tc.params;
    const rawArgs = params["arguments"];
    if (typeof rawArgs === "string" && rawArgs) {
      try { params = JSON.parse(rawArgs); } catch {}
    }
    const input = params["input"];
    if (input && typeof input === "object") params = input as Record<string, any>;

    let question = (params.question as string) || "";
    let details = (params.details as string) || "";
    let options: string[] = [];
    if (Array.isArray(params.options)) options = params.options.map(String);

    if (!question) {
      let qs = params.questions;
      // Handle double-encoded: questions might be a JSON string instead of array
      if (typeof qs === "string") {
        try { qs = JSON.parse(qs); } catch {}
      }
      if (Array.isArray(qs) && qs.length > 0) {
        const first = qs[0] as Record<string, any>;
        question = (first.question as string) || "";
        details = (first.details as string) || "";
        if (Array.isArray(first.options)) options = first.options.map(String);
      }
    }

    // Show placeholder when data hasn't arrived yet
    if (!question) {
      const debug = JSON.stringify(tc.params).substring(0, 200);
      return `<div class="question-card" id="tc-${tc.id}">
        <div class="question-card-header">
          <i data-lucide="help-circle" class="question-card-icon"></i>
          <span class="question-card-title">${t("chat.toolQuestion")}</span>
          <span class="question-card-badge">${t("chat.waitingForAnswer")}</span>
        </div>
        <div class="question-card-body">
          <div class="q-step" style="color:var(--text-muted);font-size:10px">${escapeHtml(debug)}</div>
        </div>
      </div>`;
    }

    // Done/error: show simple tool-item (like web_search completion)
    if (tc.status === "done" && tc.result) {
      return `<div class="tool-item">
        <i data-lucide="help-circle" class="tool-item-icon"></i>
        <span class="strip-name">${t("chat.toolQuestion")}</span>
        <span class="strip-summary">${escapeHtml(question.substring(0, 80))}</span>
      </div>`;
    }

    const id = `tc-${tc.id}`;
    const optsHtml = options.length
      ? `<div class="q-options">${options.map(o => {
          const s = o.indexOf("—") > 0 ? o.substring(0, o.indexOf("—")).trim() : o;
          const d = o.indexOf("—") > 0 ? o.substring(o.indexOf("—") + 1).trim() : "";
          return `<button class="q-option-card" data-value="${escapeHtml(s)}" onclick="event.stopPropagation();var inp=this.closest('.question-card-body,.agent-content').querySelector('.q-input');if(inp)inp.value=this.dataset.value;this.closest('.q-options').querySelectorAll('.q-option-card.selected').forEach(function(b){b.classList.remove('selected')});this.classList.add('selected')">
            <div class="q-option-title">${escapeHtml(s)}</div>
            ${d ? `<div class="q-option-desc">${escapeHtml(d)}</div>` : ""}
          </button>`;
        }).join("")}</div>`
      : "";

    return `<div class="question-card" id="${id}">
      <div class="question-card-header">
        <i data-lucide="help-circle" class="question-card-icon"></i>
        <span class="question-card-title">${t("chat.toolQuestion")}</span>
        <span class="question-card-badge">${t("chat.waitingForAnswer")}</span>
      </div>
      <div class="question-card-body">
        ${details ? `<div class="q-step">${escapeHtml(details)}</div>` : ""}
        <div class="q-field">
          <div class="q-field-label">${t("chat.questionField")}</div>
          <div class="q-field-text">${escapeHtml(question)}</div>
        </div>
        ${optsHtml}
        <div class="q-input-row">
          <input class="q-input" type="text" placeholder="${options.length ? t("chat.inputOtherRequirements") : t("chat.typeAnswer")}" onkeydown="if(event.key==='Enter'){event.preventDefault();window.__answerQuestion(event,this,'${id}')}" />
          <button class="q-submit" onclick="event.stopPropagation();window.__answerQuestion(event,this,'${id}')">${t("chat.submit")}</button>
        </div>
      </div>
    </div>`;
  }

  export function _agentTaskDividersImpl(this: Chat, tc: ToolCallState): Array<{ index: number; name: string; status?: string }> {
    const msgs = tc.subAgentMessages;
    if (!msgs || msgs.length === 0) return [];
    const dividers = new Map<number, { index: number; name: string; status?: string }>();
    let fallbackIndex = 0;
    for (const message of msgs) {
      if (message.mode !== "task_divider") continue;
      const index = Number.isInteger(message.taskIndex) ? message.taskIndex! : fallbackIndex;
      fallbackIndex = Math.max(fallbackIndex, index + 1);
      dividers.set(index, {
        index,
        name: message.taskName || "",
        status: message.taskStatus,
      });
    }
    return [...dividers.values()].sort((a, b) => a.index - b.index);
  }

  export function renderAssistantTextImpl(this: Chat, item: TimelineItem): string {
    const errorClass = item.hasError ? " has-error" : "";
    const msgId = item.messageId || item.id.replace("a-", "").replace(/-seg-\d+$/, "");
    return `<div class="assistant-text${errorClass}" data-id="${item.id}" data-message-id="${msgId}">
      <div class="msg-text">${renderMarkdown(item.content)}</div>
    </div>`;
  }

  export function renderErrorCardImpl(this: Chat, item: TimelineItem): string {
    const id = `ec-${item.messageId}`;
    const cat = item.errorCategory || "";
    // Category-based icon and label
    let iconSvg: string;
    let label: string;
    let extraClass = "";
    if (cat === "auth") {
      iconSvg = `<svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`;
      label = t("chat.abortedError");
      extraClass = " status-error-auth";
    } else if (cat === "rate_limit") {
      iconSvg = `<svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;
      label = t("chat.errorRateLimited");
      extraClass = " status-error-rate";
    } else if (cat === "context") {
      iconSvg = `<svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>`;
      label = t("chat.errorContext");
      extraClass = " status-error-context";
    } else if (cat === "network") {
      iconSvg = `<svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="1" y1="1" x2="23" y2="23"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/><path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><path d="M10.71 5.05A16 16 0 0 1 22.56 9"/><path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>`;
      label = t("chat.errorNetwork");
      extraClass = " status-error-network";
    } else if (cat === "server") {
      iconSvg = `<svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>`;
      label = t("chat.errorServer");
      extraClass = " status-error-server";
    } else if (cat === "tool") {
      iconSvg = `<svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`;
      label = t("chat.errorTool");
      extraClass = " status-error-tool";
    } else {
      iconSvg = `<svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`;
      label = t("chat.abortedError");
    }
    return `<div class="turn-status-card status-error${extraClass}" id="${id}">
      <div class="status-header" onclick="window.__toggleStatusCard(this, '${id}')">
        ${iconSvg}
        <span class="status-label">${label}</span>
        <svg class="status-toggle" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
      </div>
      <div class="status-body">
        <div class="status-message">${escapeHtml(item.errorMessage)}</div>
      </div>
    </div>`;
  }

  export function renderWarningCardImpl(this: Chat, item: TimelineItem): string {
    return `<div class="turn-status-card status-warning">
      <svg class="status-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      <span class="status-label">${t("chat.interrupted")}</span>
      <span class="status-detail">${escapeHtml(item.interruptedReason)}</span>
    </div>`;
  }

  export function renderBranchSwitcherImpl(this: Chat): string {
    const st = getState();
    if (st.branches.length <= 1) return "";
    const sorted = [...st.branches].sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
    const activeIdx = sorted.findIndex(b => b.id === st.activeBranchId);
    if (activeIdx < 0) return "";
    const ids = sorted.map(b => b.id).join(",");
    return `<span class="branch-switcher" data-branch-ids="${ids}" data-active-idx="${activeIdx}">
      <button class="branch-prev">‹</button>
      <span class="branch-indicator">${activeIdx + 1} / ${sorted.length}</span>
      <button class="branch-next">›</button>
    </span>`;
  }

  export function renderCompactStartingCardImpl(this: Chat, item: TimelineItem): string {
    // Non-expandable indicator — no summary, no expand/collapse, just a
    // single-line status showing that context compression is in progress.
    return `<div class="strip-item compact-starting-item" data-id="${item.id}">
      <div class="strip" style="cursor:default">
        <span class="icon-wrap">
          <i data-lucide="file-archive" class="semantic" style="width:14px;height:14px"></i>
        </span>
        <span class="strip-name">${t("chat.compressingContext")}</span>
      </div>
    </div>`;
  }

  export function renderCompactCardImpl(this: Chat, item: TimelineItem): string {
    // Clean inline divider: the compressed context itself is NEVER shown to
    // the user -- just a single-line separator marking where the earlier
    // conversation was compacted (no expand/collapse, no stats).
    return `<div class="compact-divider" data-id="${item.id}">
      <span class="compact-divider-text">${t("chat.contextCompacted")}</span>
    </div>`;
  }

  export function renderSystemMessageImpl(this: Chat, item: TimelineItem): string {
    const id = item.id;
    const expanded = this.expandedItems.has(id);
    const icon = item.kindTag === "error" ? "alert-circle" : "info";
    const label = item.kindTag === "error" ? "System notice" : "System message";
    return `<div class="strip-item system-message-strip${expanded ? " expanded" : ""}" data-id="${id}">
      <div class="strip" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        <span class="icon-wrap"><i data-lucide="${icon}" class="semantic"></i><i data-lucide="chevron-down" class="arrow"></i></span>
        <span class="strip-name">${escapeHtml(label)}</span>
        <span class="strip-summary">${escapeHtml(item.content.slice(0, 80))}</span>
      </div>
      <div class="strip-body">${escapeHtml(item.content)}</div>
    </div>`;
  }