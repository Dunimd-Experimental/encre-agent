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
import { reportUiFault } from "../core/ui_boundary.js";
import { t } from "../features/i18n.js";
import { findSlashCommand } from "../ui/slash-commands.js";
import { renderMarkdown, escapeHtml } from "./chat_markdown.js";
import { ATTACH_PLACEHOLDER_RE, decodeAttachName } from "../features/files.js";
import { getToolSummary, getToolInlineSummary, getToolBodyText, _toolStatusHtml, getAgentName, formatAgentLabel } from "./chat_tool_display.js";
import { iconFor, labelFor, metaFor, isPluginTool, toolSource, toolIconHtml } from "./tool_meta.js";
import { renderPluginError } from "./plugin_error_card.js";
import { _fmtTokens, fmtSize } from "./timeline.js";

export function renderItemHTMLImpl(this: Chat, item: TimelineItem): string {
    // Isolation boundary: a broken renderer (a plugin-supplied card, malformed
    // timeline data, an i18n miss) must degrade this one item to blank space,
    // never throw out of the timeline loop and blank the whole transcript.
    try {
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
        case "compact_notice": return this.renderCompactNotice(item);
        case "model_switch": return this.renderModelSwitchNotice(item);
        case "retry_notice": return this.renderRetryNotice(item);
        case "system_message": return this.renderSystemMessage(item);
        case "spec_card": return this.renderSpecCard(item);
        case "plan_card": return this.renderPlanCard(item);
        case "workflow": return this.renderWorkflowCard(item);
      }
      return "";
    } catch (e) {
      reportUiFault("chat-item", (item as { id?: string }).id || item.kind, e);
      return "";
    }
  }

  export function renderAIHeaderImpl(this: Chat, item: TimelineItem): string {
    return `<div class="ai-header">
      <span class="ai-name">${t("chat.encreAgent")}</span>
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
    const modeBadge = item.mode && !isTerminal ? (() => { const c = findSlashCommand(item.mode); const icon = c ? (c.icon || "") : "list-checks"; return `<span class="mode-chip" data-mode="${item.mode}">${icon ? `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i>` : ""}<span>${c ? c.title : item.mode}</span></span>`; })() : "";
    // Inline attachment placeholders: chips added mid-sentence serialize to
    // `<attach n="name" />` at their exact position, so the bubble renders
    // each chip back where the user placed it.  Refs not covered by any
    // placeholder (legacy messages, attachment-only submits whose count form
    // was stripped server-side) fall back to the head-of-bubble card row.
    const hasInlineAtt = !isSubAgent && item.content.includes("<attach ");
    let inlineBody = "";
    let headCards = "";
    if (hasInlineAtt) {
      const refs = [...(item.fileRefs || [])];
      const consumed = new Set<number>();
      const chipFor = (f: { name: string; size: number; icon: string; mime_type?: string }): string => {
        const summary = f.mime_type === "text/x-terminal"
          ? `${f.size} line${f.size !== 1 ? "s" : ""}`
          : f.mime_type === "text/x-directory" ? "folder" : "";
        return this.renderModeCard(f.icon, f.name, summary);
      };
      let last = 0;
      let m: RegExpExecArray | null;
      ATTACH_PLACEHOLDER_RE.lastIndex = 0;
      while ((m = ATTACH_PLACEHOLDER_RE.exec(item.content)) !== null) {
        inlineBody += escapeHtml(item.content.slice(last, m.index));
        const name = decodeAttachName(m[1]);
        const idx = refs.findIndex((r, i) => !consumed.has(i) && r.name === name);
        if (idx >= 0) {
          consumed.add(idx);
          inlineBody += chipFor(refs[idx]);
        } else {
          // Ref unavailable (history restored without fileRefs): render a
          // minimal chip from the placeholder itself so nothing vanishes.
          inlineBody += this.renderModeCard("paperclip", name, "");
        }
        last = m.index + m[0].length;
      }
      inlineBody += escapeHtml(item.content.slice(last));
      headCards = refs.map((f, i) => consumed.has(i) ? "" : chipFor(f)).join("");
    } else {
      headCards = item.fileRefs?.map(f => {
        const summary = f.mime_type === "text/x-terminal"
          ? `${f.size} line${f.size !== 1 ? "s" : ""}`
          : f.mime_type === "text/x-directory" ? "folder" : "";
        return this.renderModeCard(f.icon, f.name, summary);
      }).join("") || "";
    }
    const termCard = isTerminal ? this.renderModeCard("terminal", item.mode!.split(":")[1] || "Terminal", `${item.content.split("\n").length} lines`) : "";
    if (effectiveCmdMatch) {
      const cmdName = effectiveCmdMatch[1];
      const rest = cmdTagMatch ? "" : (effectiveCmdMatch[2] || "");
      const displayContent = rest ? escapeHtml(rest) : "";
      const cmd = findSlashCommand(cmdName);
      const cmdBadge = cmd
        ? `<span class="mode-chip" data-mode="${cmdName}">${cmd.icon ? `<i data-lucide="${cmd.icon}" class="chip-icon" style="width:12px;height:12px;"></i>` : ""}<span>${cmd.title}</span></span>`
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
    const contentHtml = hasInlineAtt
      ? inlineBody
      : isSubAgent
        ? escapeHtml(item.content)
        : (item.content.includes("<terminal>") || item.content.includes("<mode>")) ? "" : escapeHtml(item.content);
    return `<div class="user-item" data-user-idx="${item.index}">
      <div class="user-bubble">${modeBadge}${termCard}${headCards}${contentHtml}</div>
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
      <div class="strip-body thought-body tail-follow">${escapeHtml(item.text)}</div>
    </div>`;
  }

  export function renderToolCallImpl(this: Chat, item: TimelineItem): string {
    const tc = item.tc;
    // A plugin-sourced tool that ended in error shows the unified plugin fault
    // card instead of its normal one, so users always see the plugin, the two
    // recoveries that work and the author's own support channel without having
    // to guess which plugin broke.  The card reads the rest (version, contact,
    // author) from the shared plugin-info store, so only the identity crosses
    // this boundary.
    if (tc.isError && isPluginTool(tc.name)) {
      return renderPluginError({
        tc,
        plugin: toolSource(tc.name) || "plugin",
        expanded: this.expandedItems.has(`tc-${tc.id}`),
      });
    }
    // The card primitive is declared by the backend, not guessed from the tool
    // name: each tool ships a `card` field in its UI metadata.
    switch (metaFor(tc.name).card) {
      case "hidden": return "";
      case "agent": return this.renderAgent(tc, item.id);
      case "question":
        // Interactive card while waiting for the answer; once answered it
        // becomes the same expandable strip as thinking (click to reveal the
        // Q/A blocks).
        if ((tc.status === "done" || tc.status === "error") && tc.result) {
          return this.renderExpandableStrip(tc, item.id);
        }
        return this.renderQuestionCard(tc, item.id);
      case "info": return this.renderInfoCard(tc, item.id);
      // delete_file keeps the compact file-card.
      case "file": return this.renderFileCard(tc);
      // skill / mcp / read / web_fetch / git / notebook / apply_patch / ...
      case "item": return this.renderToolItemSimple(tc);
      // write / edit / search / grep / everything without its own shape.
      default: return this.renderExpandableStrip(tc, item.id);
    }
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
      <span class="file-card-name">${escapeHtml(path || labelFor(tc.name))}</span>
      ${errorText ? `<span class="file-card-error-text">${errorText}</span>` : ""}
    </div>`;
  }

  export function renderTerminalCardImpl(this: Chat, tc: ToolCallState, itemId: string): string {
    const id = `tc-${tc.id}`;
    const expanded = this.expandedItems.has(id);
    const term = tc.params.terminal as string | undefined;
    const termIconHtml = toolIconHtml(tc.name, "semantic");
    const summary = getToolSummary(tc);
    const hasResult = !!tc.result;
    const bodyHtml = hasResult ? getToolBodyText(tc) : "";
    return `<div class="terminal-card${expanded ? " expanded" : ""}" data-id="${id}">
      <div class="terminal-card-header" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        <span class="icon-wrap">
          ${termIconHtml}
          <i data-lucide="chevron-down" class="arrow"></i>
        </span>
        <span class="strip-name">${labelFor(tc.name, term)}</span>
        <span class="strip-summary">${summary ? `<code>${escapeHtml(summary)}</code>` : ""}</span>
      </div>
      <div class="terminal-body-slot" data-has-body="${hasResult ? "1" : "0"}">${bodyHtml ? `<div class="terminal-body tail-follow">${bodyHtml}</div>` : ""}</div>
    </div>`;
  }

  export function renderExpandableStripImpl(this: Chat, tc: ToolCallState, itemId: string): string {
    const id = `tc-${tc.id}`;
    const expanded = this.expandedItems.has(id);
    const term = tc.params.terminal as string | undefined;
    const iconGlyph = toolIconHtml(tc.name, "semantic");
    const name = labelFor(tc.name, term);
    const summary = getToolSummary(tc);
    const statusHtml = _toolStatusHtml(tc.status);
    const hasResult = !!tc.result;
    const bodyHtml = hasResult ? getToolBodyText(tc) : "";
    // Wrap the tool icon in an icon-wrap that swaps the semantic icon for
    // a chevron-down on hover, mirroring the thinking strip so the expand
    // affordance lives on the icon, not as a separate arrow on the right.
    const iconHtml = iconGlyph
      ? `<span class="icon-wrap">${iconGlyph}<i data-lucide="chevron-down" class="arrow"></i></span>`
      : "";
    const meta = metaFor(tc.name);
    // A declaration may suppress header actions; currently only "stop" exists.
    const stopBtn = meta.hide.includes("stop")
      ? ""
      : `<button class="strip-stop-btn" data-running="${tc.status === "running" && meta.body === "terminal"}" data-tool-stop="${tc.id}">${t("chat.stopRun")}</button>`;
    return `<div class="strip-item${expanded ? " expanded" : ""}" data-id="${id}">
      <div class="strip" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        ${iconHtml}
        <span class="strip-name">${name}</span>
        <span class="strip-status" data-status="${tc.status}">${statusHtml}</span>
        <span class="strip-summary">${summary ? escapeHtml(summary) : ""}</span>
        ${stopBtn}
      </div>
      <div class="strip-body-slot" data-has-body="${hasResult ? "1" : "0"}">${bodyHtml ? `<div class="strip-body tail-follow">${bodyHtml}</div>` : ""}</div>
    </div>`;
  }

  export function renderToolItemSimpleImpl(this: Chat, tc: ToolCallState): string {
    const id = `tc-${tc.id}`;
    const term = tc.params.terminal as string | undefined;
    const iconHtml = toolIconHtml(tc.name, "tool-item-icon");
    const name = labelFor(tc.name, term);
    const summary = getToolInlineSummary(tc);
    const statusHtml = _toolStatusHtml(tc.status);
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
    const id = `tc-${tc.id}`;

    // Icon slot. Plan/spec agents keep the lucide `list-checks` glyph; every
    // other sub-agent gets the 3×3 pixel loader: animated while `running`,
    // frozen into a static chevron once settled. A finished agent must never
    // keep shimmering, so the state is baked into `data-running` and kept in
    // sync incrementally by the renderer (see `chat_render_engine.ts`).
    const iconHtml = isPlanSpec
      ? `<span class="icon-wrap"><i data-lucide="list-checks" class="semantic"></i><i data-lucide="chevron-right" class="arrow"></i></span>`
      : (() => {
          const running = tc.status === "running";
          // chevron wavefront phase: delay = (col + |row - 1|) * 90ms
          const delays = [90, 180, 270, 0, 90, 180, 90, 180, 270];
          // settled frame: right-pointing chevron at (0,2) (1,1) (2,2)
          const chevron = [2, 4, 8];
          const cells = delays
            .map((d, i) => `<i${running ? ` style="--sa-d:${d}ms"` : chevron.includes(i) ? ` class="sa-chevron"` : ""}></i>`)
            .join("");
          return `<span class="icon-wrap"><span class="sa-loader" data-running="${running}">${cells}</span><i data-lucide="chevron-right" class="arrow"></i></span>`;
        })();

    // The summary floating panel is reachable from the session bar; it is NOT
    // repeated on every sub-agent card, where a "list" icon reads as a
    // per-agent action and invites clicks that only open a session-wide panel.
    const tasks = this._agentTaskDividers(tc);
    if (tasks.length > 1) {
      return tasks.map((task, idx) => {
        const taskName = task.name || `${agentName} ${idx + 1}`;
return `<div class="strip-item agent-strip" id="${id}" data-id="${id}" data-task-index="${task.index}">
          <div class="strip" onclick="window.__openSubAgentView('${tc.id}', ${task.index})" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
            ${iconHtml}
            <span class="strip-name">${escapeHtml(taskName)}</span>
          </div>
        </div>`;
      }).join("");
    }

    return `<div class="strip-item agent-strip" id="${id}" data-id="${id}">
      <div class="strip" onclick="window.__openSubAgentView('${tc.id}')" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
        ${iconHtml}
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

    // Collect EVERY question the tool asked (single `question` field or the
    // batched `questions` array) so a batched ask renders all of them.
    const qList: Array<{ text: string; details: string; options: string[] }> = [];
    const pushQ = (q: Record<string, any>) => {
      const text = String(q.question ?? "").trim();
      if (!text) return;
      qList.push({
        text,
        details: String(q.details ?? "").trim(),
        options: Array.isArray(q.options) ? q.options.map(String) : [],
      });
    };
    if (params.question) pushQ(params);
    if (qList.length === 0) {
      let qs = params.questions;
      // Handle double-encoded: questions might be a JSON string instead of array
      if (typeof qs === "string") {
        try { qs = JSON.parse(qs); } catch {}
      }
      if (Array.isArray(qs)) {
        for (const q of qs) {
          if (q && typeof q === "object") pushQ(q as Record<string, any>);
        }
      }
    }

    // Show placeholder when data hasn't arrived yet
    if (qList.length === 0) {
      const debug = JSON.stringify(tc.params).substring(0, 200);
      return `<div class="question-card" id="tc-${tc.id}">
        <div class="question-card-header">
          <i data-lucide="help-circle" class="question-card-icon"></i>
          <span class="question-card-title">${t("chat.toolQuestion")}</span>
        </div>
        <div class="question-card-body">
          <div class="q-step" style="color:var(--text-muted);font-size:10px">${escapeHtml(debug)}</div>
        </div>
      </div>`;
    }

    const id = `tc-${tc.id}`;
    const batch = qList.length > 1;
    // Restore picks made before a re-render / session switch (the .selected
    // class alone dies with the old DOM).
    const selMap = this.questionSelections.get(tc.id);
    const itemsHtml = qList.map((q, qi) => {
      const optsHtml = q.options.length
        ? `<div class="q-options">${q.options.map(o => {
            const dash = o.indexOf("—");
            const s = dash > 0 ? o.substring(0, dash).trim() : o;
            const d = dash > 0 ? o.substring(dash + 1).trim() : "";
            return `<button type="button" class="q-option-card${selMap?.[qi] === s ? " selected" : ""}" data-value="${escapeHtml(s)}" onclick="event.stopPropagation();window.__selectQuestionOption(this)">
              <span class="q-option-text">
                <span class="q-option-title">${escapeHtml(s)}</span>
                ${d ? `<span class="q-option-desc">${escapeHtml(d)}</span>` : ""}
              </span>
            </button>`;
          }).join("")}</div>`
        : "";
      const placeholder = escapeHtml(q.options.length ? t("chat.questionOrType") : t("chat.typeAnswer"));
      // Stepped ask: only one question is visible at a time.  Every item is
      // rendered up front (so answers survive switching back and forth) and
      // visibility is toggled via the hidden attribute.  "Q N" sits on the
      // SAME line as the question text, same text style — no separate label
      // row, no model rationale line.
      return `<div class="q-item"${batch && qi > 0 ? " hidden" : ""}>
        <div class="q-field">
          <div class="q-field-text">${batch ? `<span class="q-field-label">${escapeHtml(t("chat.questionField"))} ${qi + 1}</span> ` : ""}${escapeHtml(q.text)}</div>
        </div>
        ${optsHtml}
        <div class="q-custom-row">
          <input class="q-input" type="text" placeholder="${placeholder}"
            oninput="event.stopPropagation();this.closest('.q-item').classList.remove('q-missing')"
            onkeydown="if(event.key==='Enter'){event.preventDefault();window.__answerQuestion(event,this,'${id}')}" />
          ${batch ? "" : `<button type="button" class="q-submit" onclick="event.stopPropagation();window.__answerQuestion(event,this,'${id}')">${t("chat.submit")}</button>`}
        </div>
      </div>`;
    }).join("");

    return `<div class="question-card question-card--ask" id="${id}" data-batch="${batch ? 1 : 0}">
      <div class="question-card-header">
        <i data-lucide="help-circle" class="question-card-icon"></i>
        <span class="question-card-title">${t("chat.toolQuestion")}</span>
      </div>
      <div class="question-card-body">
        ${itemsHtml}
        ${batch ? `<div class="q-footer">
          <button type="button" class="q-submit" data-nav="-1" onclick="event.stopPropagation();window.__qNav(this,-1)" hidden>${t("chat.questionPrev")}</button>
          <button type="button" class="q-submit" data-nav="1" onclick="event.stopPropagation();window.__qNav(this,1)">${t("chat.questionNext")}</button>
          <button type="button" class="q-submit" data-qsubmit onclick="event.stopPropagation();window.__answerQuestion(event,this,'${id}')" hidden>${t("chat.submit")}</button>
        </div>` : ""}
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

  /**
   * Assistant text block.
   *
   * `raw` renders escaped plain text instead of markdown. Boot uses it to get
   * a long restored transcript on screen immediately (< 1 ms per block),
   * then upgrades each block to full markdown during idle time -- parsing
   * every historical message in one synchronous pass is what froze the whole
   * window (title bar included) on startup and session switches.
   */
  export function renderAssistantTextImpl(this: Chat, item: TimelineItem, raw = false): string {
    const errorClass = item.hasError ? " has-error" : "";
    const msgId = item.messageId || item.id.replace("a-", "").replace(/-seg-\d+$/, "");
    const body = raw
      ? `<div class="msg-text msg-text--raw">${escapeHtml(item.content)}</div>`
      : `<div class="msg-text">${renderMarkdown(item.content)}</div>`;
    return `<div class="assistant-text${errorClass}" data-id="${item.id}" data-message-id="${msgId}">
      ${body}
    </div>`;
  }

  export function renderErrorCardImpl(this: Chat, item: TimelineItem): string {
    return `<div class="turn-line turn-line--error">
      <div class="turn-line__title">${t("chat.abortedError")}</div>
      <div class="turn-line__body">${escapeHtml(item.errorMessage)}</div>
    </div>`;
  }

  export function renderWarningCardImpl(this: Chat, item: TimelineItem): string {
    // The detail is intentionally not shown: an interrupted turn says only
    // that it stopped. The resume affordance is appended by the timeline
    // builder (showContinue) and only while this card is the tail of the
    // transcript — once anything lands below it the button is gone.
    const continueBtn = item.showContinue
      ? `<button class="turn-line__action" data-turn-continue>${t("chat.continueRun")}</button>`
      : "";
    return `<div class="turn-line turn-line--warning">
      <div class="turn-line__head">
        <span class="turn-line__title">${t("chat.interrupted")}</span>
        ${continueBtn}
      </div>
    </div>`;
  }

  export function renderBranchSwitcherImpl(this: Chat, group?: { ids: string[]; activeIdx: number }): string {
    // Per-fork-point sibling group built by buildTimeline: only the branches
    // that alternate at THIS fork point are listed, so the indicator reads
    // "x / y" within the turn's own alternatives and prev/next can never jump
    // the conversation to an unrelated fork's tail. Without a group there is
    // nothing meaningful to alternate -- render nothing.
    if (!group || group.ids.length < 2) return "";
    const ids = group.ids;
    const activeIdx = group.activeIdx;
    if (activeIdx < 0) return "";
    return `<span class="branch-switcher" data-branch-ids="${ids.join(",")}" data-active-idx="${activeIdx}">
      <button class="branch-prev">‹</button>
      <span class="branch-indicator">${activeIdx + 1} / ${ids.length}</span>
      <button class="branch-next">›</button>
    </span>`;
  }

  /** Shared notice frame: centered text between two divider lines, with an
   *  optional info icon (tooltip) -- the same visual language for context
   *  compaction and model switches. */
  function noticeFrame(textHtml: string, tipHtml: string, extraClass = ""): string {
    const tip = tipHtml
      ? `<span class="timeline-notice-info" data-tooltip="${tipHtml.replace(/"/g, "&quot;")}"><i data-lucide="info" class="lucide"></i></span>`
      : "";
    return `<div class="timeline-notice${extraClass}">
      <span class="timeline-notice-line"></span>
      <span class="timeline-notice-text">${textHtml}</span>
      ${tip}
      <span class="timeline-notice-line"></span>
    </div>`;
  }

  export function renderCompactStartingCardImpl(this: Chat, item: TimelineItem): string {
    // Live "compressing" state of the notice line: same frame as the
    // completed notice, plus the left-to-right shimmer under the text.
    // It flips in place (same anchor) when the compaction finishes.
    return `<div class="timeline-notice timeline-notice--compacting" data-id="${item.id}">
      <span class="timeline-notice-line"></span>
      <span class="timeline-notice-text">${t("chat.compressingContext")}</span>
      <span class="timeline-notice-line"></span>
      <span class="timeline-notice-shimmer" aria-hidden="true"></span>
    </div>`;
  }

  // Transient retry line: a failed model call is being retried.  Same red
  // vertical-line family as the error card, but it disappears the moment the
  // retried attempt produces output (the timeline drops the item).  A model
  // fallback (scope "model") is not a wait, so it shows the switch instead of
  // an attempt countdown.
  //
  // The reason is the payload's own message field, never the serialized blob:
  // the provider replies with things like
  //   HTTP 429: {"error":{"message":"inference exceeds tpm/rpm limit","type":...}}
  // and the line shows "inference exceeds tpm/rpm limit" -- the same "pick the
  // relevant field, don't dump the object" rule a tool strip follows.  The
  // untouched text stays reachable as a tooltip whenever the shown value
  // differs from it.
  const RETRY_MESSAGE_KEYS = [
    "message", "detail", "error_description", "reason", "description", "msg", "error",
  ];

  /** First message-like string in a payload, unwrapping nested objects/arrays. */
  function pickMessageField(node: unknown, depth = 0): string {
    if (depth > 4 || node === null || typeof node !== "object") return "";
    if (Array.isArray(node)) {
      for (const entry of node) {
        const found = pickMessageField(entry, depth + 1);
        if (found) return found;
      }
      return "";
    }
    const obj = node as Record<string, unknown>;
    for (const key of RETRY_MESSAGE_KEYS) {
      const value = obj[key];
      if (typeof value === "string" && value.trim()) return value.trim();
      if (value && typeof value === "object") {
        const nested = pickMessageField(value, depth + 1);
        if (nested) return nested;
      }
    }
    return "";
  }

  export function pickRetryReason(message: string): string {
    const raw = (message || "").trim();
    // Providers prefix the payload with their own text ("HTTP 429: {...}"), so
    // the object does not start at index 0.  Try every `{`/`[` offset in order
    // and take the first that parses -- a stray bracket in the prefix (e.g.
    // "HTTP 429 [retry] {...}") simply fails to parse and is skipped.
    const starts: number[] = [];
    for (let i = 0; i < raw.length; i++) {
      const ch = raw[i];
      if (ch === "{" || ch === "[") starts.push(i);
    }
    for (const start of starts) {
      try {
        const picked = pickMessageField(JSON.parse(raw.slice(start)));
        if (picked) return picked;
      } catch {
        // Not parseable at this offset -- try the next candidate.
      }
    }
    return raw;
  }

  export function renderRetryNoticeImpl(this: Chat, item: TimelineItem): string {
    const isSwitch = item.scope === "model";
    const maxLabel = item.maxAttempts > 0 ? String(item.maxAttempts) : "∞";
    let titleHtml: string;
    if (isSwitch) {
      titleHtml = escapeHtml(t("chat.switchingModel"));
    } else {
      const remaining = item.deadline > 0
        ? Math.max(0, Math.ceil((item.deadline - Date.now()) / 1000))
        : 0;
      const countdown = remaining > 0
        ? `<span class="turn-line__countdown">${escapeHtml(t("chat.retryCountdown", { seconds: remaining }))}</span>`
        : "";
      titleHtml = escapeHtml(`${t("chat.retrying")} · ${t("chat.retryAttempt", { attempt: item.attempt, max: maxLabel })}`) + countdown;
    }
    const raw = (item.message || "").trim();
    const reason = pickRetryReason(item.message);
    const tip = raw && reason !== raw
      ? ` data-tooltip="${escapeHtml(raw).replace(/"/g, "&quot;")}"`
      : "";
    const body = reason
      ? `<div class="turn-line__body"${tip}>${escapeHtml(reason)}</div>`
      : "";
    const deadlineAttr = item.deadline > 0 ? ` data-retry-deadline="${item.deadline}"` : "";
    return `<div class="turn-line turn-line--error" data-id="${item.id}"${deadlineAttr}>
      <div class="turn-line__head">
        <span class="turn-line__title">${titleHtml}</span>
      </div>
      ${body}
    </div>`;
  }

  export function renderCompactCardImpl(this: Chat, item: TimelineItem): string {
    // Legacy inline divider from a compact_marker message (kept for
    // sessions persisted before notices were recorded).
    return noticeFrame(t("chat.contextCompacted"), "");
  }

  export function renderCompactNoticeImpl(this: Chat, item: TimelineItem): string {
    // Completed compaction record: plain text (no shimmer), with the
    // before/after token + message counts in the info tooltip.
    const tip = t("chat.compactStats", {
      old: _fmtTokens(item.oldTokens),
      fresh: _fmtTokens(item.newTokens),
      oldCount: String(item.oldCount),
      newCount: String(item.newCount),
    });
    return noticeFrame(t("chat.contextCompacted"), escapeHtml(tip));
  }

  export function renderModelSwitchNoticeImpl(this: Chat, item: TimelineItem): string {
    // Model-switch record, two distinct wordings:
    // - manual selection: "模型已从 A 更改为 B" with a "switched manually"
    //   tooltip;
    // - automatic pool cycling: "模型池已自动从 A 切换到 B", and the info
    //   tooltip shows the SAME note the model-pool settings' info icon
    //   shows (server-side prompt cache is isolated per model) -- one
    //   consistent explanation in both places.
    if (item.source === "pool") {
      const text = t("chat.modelSwitchedPoolAuto", {
        from: item.fromModel || "—",
        to: item.toModel || "—",
      });
      return noticeFrame(escapeHtml(text), escapeHtml(t("settings.modelPoolFallbackCacheHint")));
    }
    const text = t("chat.modelSwitchedFrom", {
      from: item.fromModel || "—",
      to: item.toModel || "—",
    });
    return noticeFrame(escapeHtml(text), escapeHtml(t("chat.modelSwitchReasonManual")));
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