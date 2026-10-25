// @ts-nocheck -- methods relocated from the Chat class in chat.ts; private
// members are reached through the instance (`self`). Runtime semantics are
// byte-identical to the original in-class implementations.
import type { Chat } from "./chat.js";
import type { TimelineItem } from "./timeline.js";
import { getState } from "../core/state.js";
import { t } from "../features/i18n.js";
import { escapeHtml } from "./chat_markdown.js";
import { getFileIcon } from "../features/files.js";

export function renderSpecCardImpl(self: Chat, item: Extract<TimelineItem, { kind: "spec_card" }>): string {
  const spec = item.spec;
  const status = spec.status;
  const isApproved = status === "approved";
  const isRejected = status === "rejected";
  const isReview = status === "review" || status === "draft";
  const sessionId = getState().sessionId;
  const cardId = `spec-card-${sessionId}`;

  // Section list as expandable file rows
  const sectionsHtml = spec.sections.map((s, i) => {
    const sid = `spec-sec-${i}`;
    const open = self.expandedItems.has(sid);
    return `<div class="review-file${open ? " expanded" : ""}" data-review-toggle="${sid}">
      <div class="review-file-row">
        <span class="review-file-icon"><i data-lucide="file-text"></i></span>
        <span class="review-file-name">${escapeHtml(s.title)}</span>
        <i data-lucide="chevron-down" class="review-file-arrow"></i>
      </div>
      <div class="review-file-body">${escapeHtml(s.content)}</div>
    </div>`;
  }).join("");

  const feedbackHtml = spec.feedback ? `<div class="review-feedback"><strong>Feedback:</strong> ${escapeHtml(spec.feedback)}</div>` : "";

  let bodyHtml: string;
  if (isReview) {
    bodyHtml = `<div class="question-card-body">
      <div class="q-step">${escapeHtml(t("chat.reviewSpecSub") || "If it does not match your intent, review and edit the files, or enter guidance in the input box.")}</div>
      <div class="q-field">
        <div class="q-field-label">${t("chat.reviewArtifact") || "Files"}</div>
        <div class="q-field-text">${sectionsHtml}</div>
      </div>
      ${feedbackHtml}
      <div class="q-actions">
        <button class="q-action-btn q-action-btn--ghost" data-spec-reject="${sessionId}">${t("chat.reviewCancel") || "Cancel"}</button>
        <button class="q-action-btn q-action-btn--primary" data-spec-approve="${sessionId}">${t("chat.reviewExecute") || "Execute"}</button>
      </div>
    </div>`;
  } else if (isApproved) {
    bodyHtml = `<div class="question-card-body">
      <div class="q-step">${escapeHtml(t("chat.reviewSpecMain") || "The specification has been generated.")}</div>
      <div class="q-field">
        <div class="q-field-label">${t("chat.specDocument")}</div>
        <div class="q-field-text">${sectionsHtml}</div>
      </div>
      ${feedbackHtml}
      <div class="review-state review-state--ok"><i data-lucide="check-circle"></i> ${t("chat.reviewExecuted") || "Executed"}</div>
    </div>`;
  } else if (isRejected) {
    bodyHtml = `<div class="question-card-body">
      <div class="q-step">${escapeHtml(t("chat.reviewSpecMain") || "The specification has been generated.")}</div>
      <div class="q-field">
        <div class="q-field-label">${t("chat.specDocument")}</div>
        <div class="q-field-text">${sectionsHtml}</div>
      </div>
      ${feedbackHtml}
      <div class="review-state review-state--no"><i data-lucide="x-circle"></i> ${t("chat.reviewCancelled") || "Cancelled"}</div>
    </div>`;
  } else {
    bodyHtml = `<div class="question-card-body">${sectionsHtml}</div>`;
  }

  // Header mirrors the question card: icon + fixed tool name. The long
  // status copy lives in the body (q-step), not in the header.
  return `<div class="question-card" id="${cardId}">
    <div class="question-card-header">
      <i data-lucide="file-text" class="question-card-icon"></i>
      <span class="question-card-title">Spec Review</span>
    </div>
    ${bodyHtml}
  </div>`;
}

export function renderPlanCardImpl(self: Chat, item: Extract<TimelineItem, { kind: "plan_card" }>): string {
  const review = item.review;
  const status = review.status === "approved" || review.status === "rejected" || review.status === "cancelled"
    ? review.status
    : "review";
  const isSpec = review.mode === "spec";
  const rid = review.review_id || "";
  // One-shot card: it exists only while the decision is owed. `plan_review`
  // clears the slot on every terminal status, and `session_ready` only
  // replays pending ones, so a decided record never renders a card.
  if (status !== "review") return "";
  const cardId = `plan-card-${rid}`;
  const title = isSpec ? t("chat.reviewSpecMain") : t("chat.reviewPlanMain");

  // Artifact rows come from the model's submit_review call.  Records
  // persisted before that flow only carry the three fixed section names,
  // which still resolve against the stored review directory.
  let files: { path: string; label: string }[];
  if (review.files && review.files.length) {
    files = review.files;
  } else {
    const dir = (review.dir_path || "").replace(/[/\\]+$/, "");
    files = ["plan.md", "steps.md", "checklist.md"].map(n => ({
      path: dir ? `${dir}/${n}` : "", label: n,
    }));
  }

  // Artifact cards: the file's own name is the title (the model's free-form
  // label is not shown -- it decorated the row with things like "（一页）"),
  // and the full path rides on the tooltip.
  const filesHtml = `<div class="model-table">${files.map(f => {
    const path = f.path || "";
    const name = path ? (path.split(/[/\\]/).pop() || f.label) : f.label;
    return `
    <div class="model-table-row model-table-row--file"${path ? ` data-md-path="${escapeHtml(path)}" title="${escapeHtml(path)}"` : ""}>
      <div class="model-table-cell model-cell-name">
        <span class="review-file-icon"><img src="${getFileIcon(path || name)}" alt=""></span>
        <span class="model-name-text">${escapeHtml(name)}</span>
      </div>
      <i data-lucide="arrow-up-right" class="lucide review-file-open"></i>
    </div>`;
  }).join("")}
  </div>`;

  const actionsHtml = `<div class="q-actions">
        <button class="q-action-btn q-action-btn--ghost" onclick="event.stopPropagation();window.__reviewDecision('reject','${escapeHtml(rid)}')">${t("chat.reviewCancel")}</button>
        <button class="q-action-btn q-action-btn--primary" onclick="event.stopPropagation();window.__reviewDecision('approve','${escapeHtml(rid)}')">${t("chat.reviewExecute")}</button>
      </div>`;

  // Header mirrors the question card: icon + fixed tool name in plain
  // English (same convention as "Question"), not translated. The long
  // status copy ("plan generated, proceed?") belongs to the body.
  const headerTitle = isSpec ? "Spec Review" : "Plan Review";

  return `<div class="question-card review-card" id="${cardId}" data-review-id="${escapeHtml(rid)}">
    <div class="question-card-header">
      <i data-lucide="${isSpec ? "file-text" : "list-checks"}" class="question-card-icon"></i>
      <span class="question-card-title">${escapeHtml(headerTitle)}</span>
    </div>
    <div class="question-card-body">
      <div class="q-step">${escapeHtml(title)}</div>
      ${filesHtml}
      ${actionsHtml}
    </div>
  </div>`;
}

export function renderWorkflowCardImpl(_self: Chat, _item: Extract<TimelineItem, { kind: "workflow" }>): string {
  const wf = getState().workflowState;
  if (!wf) return "";
  const pct = wf.totalTasks > 0 ? Math.round((wf.completedCount + wf.failedCount + wf.skippedCount) / wf.totalTasks * 100) : 0;
  const doneCount = wf.completedCount + wf.failedCount + wf.skippedCount;
  const status = wf.active ? "running" : (wf.success ? "completed" : "failed");
  const statusIcon = status === "running" ? "loader-circle" : status === "completed" ? "check-circle" : "x-circle";
  const statusColor = status === "running" ? "#3b82f6" : status === "completed" ? "#22c55e" : "#ef4444";
  const statusLabel = status === "running" ? t("chat.workflowRunning") || "Running" : status === "completed" ? t("chat.workflowDone") || "Completed" : t("chat.workflowFailed") || "Failed";
  const taskList = wf.tasks.map(task => {
    const dotCls = task.status === "completed" ? "wf-dot--done"
      : task.status === "running" ? "wf-dot--running"
      : task.status === "failed" ? "wf-dot--failed"
      : task.status === "skipped" ? "wf-dot--skipped"
      : "wf-dot--pending";
    const dot = `<span class="wf-dot ${dotCls}" data-tooltip="${escapeHtml(task.taskName)}: ${task.status}"></span>`;
    return `<div class="wf-task">
      ${dot}
      <span class="wf-task-name">${escapeHtml(task.taskName || task.taskId)}</span>
      <span class="wf-task-status">${task.status}</span>
    </div>`;
  }).join("");
  return `<div class="workflow-card" id="wf-${wf.workflowId}">
    <div class="workflow-card-header">
      <i data-lucide="${statusIcon}" class="workflow-card-icon" style="color:${statusColor}"></i>
      <span class="workflow-card-goal">${escapeHtml(wf.goal)}</span>
      <span class="workflow-card-badge" style="background:${statusColor}20;color:${statusColor}">${statusLabel}</span>
    </div>
    <div class="workflow-progress-bar">
      <div class="workflow-progress-fill" style="width:${pct}%;background:${statusColor}"></div>
    </div>
    <div class="workflow-progress-text">${doneCount}/${wf.totalTasks} tasks — ${wf.completedCount} done, ${wf.failedCount} failed, ${wf.skippedCount} skipped</div>
    <div class="workflow-task-list">${taskList}</div>
  </div>`;
}
