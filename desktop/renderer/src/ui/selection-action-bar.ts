/**
 * Selection action bar — the small horizontal strip that appears when a drag
 * selection ends inside a content surface (the model's reply, previews, diffs,
 * the code editor, the terminal) and offers 复制 / 添加到对话. The composer is
 * not one of them: selecting your own draft shows nothing.
 *
 * Right-click is untouched: the strip only ever appears at the end of a
 * drag-select, and both routes offer the same actions.
 *
 * ONE-SHOT BY DESIGN: the strip is bound to a single selection gesture. Once
 * it is dismissed — the pointer leaves the selection (or the strip), the view
 * scrolls, a key is pressed, another click lands — that gesture is burnt and
 * hovering back over the same highlight will NOT bring it back. Only a new
 * drag-select produces a new strip.
 *
 * DOM selections are read from `window.getSelection()`. The code editor and
 * the terminal keep their own selection models (Monaco has no DOM range, and
 * xterm.css sets `user-select: none`), so those two call
 * `showSelectionActionBarFor()` with the text and the rect they measured.
 */

import { t } from "../features/i18n.js";
import { showToast } from "../core/state.js";
// The project-wide copy→checkmark beat (glyph fades out, swaps to a check,
// fades back in). Same helper the chat tool actions, the notification panel and
// the splash screen use — the strip does not invent its own feedback UI.
import { flashCopyButton } from "../chat/chat_tool_display.js";

const BAR_ID = "selection-action-bar";

/**
 * Surfaces that carry text a reader may want to copy. Anything else is chrome
 * — see the selection policy block at the top of styles.css, which keeps the
 * rest of the shell unselectable in the first place.
 *
 * The composer (`#prompt-input`) is deliberately NOT in this list: its text is
 * being written, not read, and a strip hanging over your own draft only gets in
 * the way. Selecting inside it produces no strip at all.
 */
const CONTENT_ROOTS = [
  ".msg-text",
  ".user-bubble",
  ".markdown-preview-body",
  ".markdown-preview-raw",
  ".si-panel-body",
  ".si-office-viewer",
  ".diff-content",
  ".diff-split-scroll-side",
  ".si-review-diff",
  ".si-review-diff-content",
  ".log-row",
  ".log-cell",
  ".log-msg-text",
  ".automation-detail-card",
  ".automation-detail-scroll",
];

/** Where "add to conversation" should wrap the text as a fenced block. */
const CODE_ROOTS = "pre, code, .diff-content, .diff-split-scroll-side, .monaco-editor, .xterm";

export interface SelectionActionTarget {
  text: string;
  /** "code" wraps the text in a ``` fence when added to the conversation. */
  kind?: "text" | "code";
  /** Hide the add-to-conversation button (the composer itself). */
  allowAdd?: boolean;
  /** Viewport coordinates the strip hangs below. */
  rect: { left: number; top: number; right: number; bottom: number };
}

interface Target extends Required<Pick<SelectionActionTarget, "text" | "kind" | "allowAdd">> {
  rect: SelectionActionTarget["rect"];
  /** Monaco / xterm: no DOM range to validate against. */
  external: boolean;
}

const GAP = 8;
const EDGE = 8;
/** How far the pointer may roam before the strip burns (around the selection). */
const SLACK = 6;
/** How long the check-mark beat stays on screen before the strip folds away.
 *  Matches `flashCopyButton`'s own 2s revert clock. */
const FLASH_MS = 2000;

let _installed = false;
let _bar: HTMLElement | null = null;
let _target: Target | null = null;
let _guard: { l: number; t: number; r: number; b: number } | null = null;
let _down: { x: number; y: number } | null = null;
let _moveRaf = 0;
/** While a button's check-mark beat is on screen the strip refuses to burn:
 *  the click's own focus/selection churn — and a stray pointer move — must not
 *  cut short the feedback the user just asked for. */
let _flashLockedUntil = 0;
/** Orphans a pending flash dismissal when a new gesture re-paints the bar. */
let _flashToken = 0;

/** Installs the strip once, at boot. */
export function installSelectionActionBar(): void {
  if (_installed) return;
  _installed = true;
  document.addEventListener("mousedown", onMouseDown, true);
  document.addEventListener("mouseup", onMouseUp, true);
  document.addEventListener("selectionchange", onSelectionChange);
  document.addEventListener("mousemove", onMouseMove, { passive: true, capture: true });
  document.addEventListener("scroll", () => dismissSelectionActionBar(), { capture: true, passive: true });
  document.addEventListener("keydown", () => dismissSelectionActionBar(), true);
  // A context menu supersedes the strip: the menu carries its own copy item, so
  // leaving the strip on screen would offer the same command twice.
  document.addEventListener("contextmenu", () => dismissSelectionActionBar(), true);
  window.addEventListener("resize", () => dismissSelectionActionBar());
  window.addEventListener("blur", () => dismissSelectionActionBar());
}

/** Hides the strip and burns the gesture it belonged to. Ignored while a
 *  check-mark beat is still running (see `_flashLockedUntil`). */
export function dismissSelectionActionBar(): void {
  if (Date.now() < _flashLockedUntil) return;
  _target = null;
  _guard = null;
  if (_bar) _bar.classList.add("hidden");
}

/** Shows the strip for a selection that does not exist in the DOM
 *  (the Monaco editor, the xterm terminal). Same one-shot rules. */
export function showSelectionActionBarFor(src: SelectionActionTarget): void {
  if (!src.text.trim()) {
    dismissSelectionActionBar();
    return;
  }
  open({
    text: src.text,
    kind: src.kind ?? "text",
    allowAdd: src.allowAdd ?? true,
    rect: src.rect,
    external: true,
  });
}

function onMouseDown(e: MouseEvent): void {
  // Pressing the strip must not collapse the selection the action needs.
  if (_bar && e.target instanceof Node && _bar.contains(e.target)) {
    e.preventDefault();
    return;
  }
  // Only the primary button arms a gesture. A right-press is the context-menu
  // path: the menu carries its own copy item, so the strip must never open from
  // it (that was the "only shows up on right-click" bug) and any gesture left
  // over from the drag must not survive into it either.
  if (e.button !== 0) {
    _down = null;
    dismissSelectionActionBar();
    return;
  }
  _down = { x: e.clientX, y: e.clientY };
  dismissSelectionActionBar();
}

function onMouseUp(e: MouseEvent): void {
  if (_bar && e.target instanceof Node && _bar.contains(e.target)) return;
  const down = _down;
  _down = null;
  if (e.button !== 0 || !down) return;
  // A click (or a tiny tremor) is not a drag-select: never re-open a strip the
  // user already dismissed by clicking inside the same highlight.
  if (Math.abs(e.clientX - down.x) < 4 && Math.abs(e.clientY - down.y) < 4) return;
  // Let the engine commit the selection before reading it back.
  window.setTimeout(() => open(null), 0);
}

function onSelectionChange(): void {
  if (!_target || _target.external) return;
  if (!_bar || _bar.classList.contains("hidden")) return;
  const sel = window.getSelection();
  if (!sel || sel.toString() !== _target.text) dismissSelectionActionBar();
}

function onMouseMove(e: MouseEvent): void {
  if (!_guard || !_bar || _bar.classList.contains("hidden") || _moveRaf) return;
  const x = e.clientX;
  const y = e.clientY;
  _moveRaf = requestAnimationFrame(() => {
    _moveRaf = 0;
    const g = _guard;
    if (!g) return;
    if (x < g.l || x > g.r || y < g.t || y > g.b) dismissSelectionActionBar();
  });
}

function open(forced: Target | null): void {
  const target = forced ?? readDomSelection();
  if (!target) {
    dismissSelectionActionBar();
    return;
  }
  // A fresh gesture supersedes any beat still on screen: drop the lock and
  // orphan the old timer so it cannot hide the strip this call is about to
  // paint.
  _flashToken++;
  _flashLockedUntil = 0;
  _target = target;
  paint(target);
}

/** Reads the live DOM selection, or null when it is not a selection the shell
 *  is willing to act on (empty, collapsed, or outside every content root). */
function readDomSelection(): Target | null {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  const start = range.startContainer;
  const el = start.nodeType === 1 ? (start as HTMLElement) : start.parentElement;
  if (!el) return null;
  if (_bar && _bar.contains(el)) return null;
  const root = findContentRoot(el);
  if (!root) return null;
  const text = sel.toString();
  if (!text.trim()) return null;
  const full = range.getBoundingClientRect();
  // The strip hangs under the LAST painted line, not under the union box —
  // a union box on a long multi-line selection sits well below its own text.
  const rects = range.getClientRects();
  let last: { left: number; top: number; right: number; bottom: number } | null = null;
  for (let i = 0; i < rects.length; i++) {
    const r = rects[i];
    if (r.width > 0 && r.height > 0) {
      last = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
    }
  }
  const kind: "text" | "code" = el.closest(CODE_ROOTS) ? "code" : "text";
  // Every remaining content root is somewhere the text can be sent back into
  // the conversation — the one place it could not (the composer) is excluded
  // from CONTENT_ROOTS above. Monaco / xterm targets still pass `allowAdd`
  // explicitly when their surface is read-only.
  const allowAdd = true;
  if (last) {
    return { text, kind, allowAdd, rect: { left: full.left, top: last.top, right: full.right, bottom: last.bottom }, external: false };
  }
  return { text, kind, allowAdd, rect: { left: full.left, top: full.top, right: full.right, bottom: full.bottom }, external: false };
}

function findContentRoot(el: HTMLElement): HTMLElement | null {
  let cur: HTMLElement | null = el;
  while (cur && cur !== document.body) {
    for (const selector of CONTENT_ROOTS) {
      if (cur.matches(selector)) return cur;
    }
    cur = cur.parentElement;
  }
  return null;
}

function ensureBar(): HTMLElement {
  if (_bar && _bar.isConnected) return _bar;
  const bar = document.createElement("div");
  bar.id = BAR_ID;
  bar.className = "selection-action-bar hidden";
  bar.setAttribute("role", "toolbar");
  bar.innerHTML =
    `<button type="button" class="sab-btn" data-act="copy" data-original-icon="copy">` +
    `<i data-lucide="copy" class="lucide"></i>` +
    `<span>${escapeHtml(t("chat.copy"))}</span>` +
    `</button>` +
    `<button type="button" class="sab-btn" data-act="add" data-original-icon="corner-down-left">` +
    `<i data-lucide="corner-down-left" class="lucide"></i>` +
    `<span>${escapeHtml(t("chat.addToConversation"))}</span>` +
    `</button>`;
  bar.addEventListener("mousedown", (e) => e.preventDefault());
  bar.addEventListener("click", (e) => {
    const btn = e.target instanceof Element ? e.target.closest("button[data-act]") : null;
    const target = _target;
    if (!btn || !target) return;
    const act = (btn as HTMLElement).dataset.act;
    if (act === "copy") copyText(target.text, btn as HTMLElement);
    else if (act === "add") addToConversation(target, btn as HTMLElement);
    // Deliberately NOT dismissed here: the strip stays put while the button
    // plays its check-mark beat and folds away itself once that beat is over.
  });
  document.body.appendChild(bar);
  // `vendor/lucide.min.js` is loaded globally by index.html (the same call the
  // rest of the renderer makes). Scoped to the bar on purpose: an unscoped
  // createIcons() re-walks the whole document.
  (window as any).lucide?.createIcons({ root: bar });
  _bar = bar;
  return bar;
}

/** The shared "done" beat: the label says so, `flashCopyButton` swaps the glyph
 *  to a check (fade out → swap → fade in), and only once that beat is over does
 *  the one-shot strip fold away. */
function flashDone(btn: HTMLElement, labelKey: string): void {
  beatButtons(btn, labelKey);
  _flashLockedUntil = Date.now() + FLASH_MS;
  const token = ++_flashToken;
  flashCopyButton(btn);
  window.setTimeout(() => {
    if (token !== _flashToken) return;
    _flashLockedUntil = 0;
    dismissSelectionActionBar();
  }, FLASH_MS);
}

/** The done beat, in one layout pass: the pressed button grows to fit its done
 *  label while the OTHER button is pushed out of the strip, so what is left is
 *  just the button you pressed (已复制 / 已添加 + its check mark). Every width
 *  is pinned inline first — a flex item's `width: auto` gives the transition
 *  nothing to interpolate from — and `restoreButton()` releases the pins for
 *  the next gesture. */
function beatButtons(btn: HTMLElement, labelKey: string): void {
  const bar = _bar;
  const label = btn.querySelector("span");
  if (!bar) {
    if (label) label.textContent = t(labelKey);
    return;
  }
  // Measure what the done label wants to be with the pin released, then put the
  // button back to the width it has right now so the grow has a start value.
  const wNow = btn.offsetWidth;
  btn.style.width = "auto";
  if (label) label.textContent = t(labelKey);
  const wDone = btn.offsetWidth;
  btn.style.width = `${wNow}px`;
  void btn.offsetWidth;
  btn.classList.add("sab-grow");
  btn.style.width = `${wDone}px`;

  const others = Array.from(bar.querySelectorAll<HTMLElement>(".sab-btn")).filter((b) => b !== btn);
  for (const other of others) {
    other.style.width = `${other.offsetWidth}px`;
    other.classList.add("sab-out");
  }
  if (!others.length) return;
  // One forced reflow commits the pinned widths as the transitions' start
  // values; the collapse then runs off the class + the inline `0px` below.
  void bar.offsetWidth;
  for (const other of others) other.style.width = "0px";
}

/** Puts one button back to the glyph and label it was built with. */
function restoreButton(btn: HTMLElement | null, icon: string, label: string): void {
  if (!btn) return;
  btn.classList.remove("copying", "copy-flash", "is-fading", "sab-out", "sab-grow");
  btn.style.width = "";
  btn.style.overflow = "";
  const glyph = btn.querySelector("[data-lucide]");
  if (glyph) glyph.setAttribute("data-lucide", icon);
  const text = btn.querySelector("span");
  if (text) text.textContent = label;
}

/** The bar is reused for every gesture, so a beat left over from the previous
 *  click — or one that was cut short — must not leak into the new one. */
function resetBar(bar: HTMLElement): void {
  restoreButton(bar.querySelector<HTMLElement>('button[data-act="copy"]'), "copy", t("chat.copy"));
  restoreButton(bar.querySelector<HTMLElement>('button[data-act="add"]'), "corner-down-left", t("chat.addToConversation"));
  (window as any).lucide?.createIcons({ root: bar });
}

function paint(target: Target): void {
  const bar = ensureBar();
  resetBar(bar);
  const addBtn = bar.querySelector<HTMLElement>('button[data-act="add"]');
  if (addBtn) {
    if (target.allowAdd) addBtn.classList.remove("hidden");
    else addBtn.classList.add("hidden");
  }
  bar.classList.remove("hidden");
  const w = bar.offsetWidth;
  const h = bar.offsetHeight;
  let top = target.rect.bottom + GAP;
  if (top + h > window.innerHeight - EDGE) {
    top = Math.max(EDGE, target.rect.top - h - GAP);
  }
  const center = target.rect.left + (target.rect.right - target.rect.left) / 2;
  let left = center - w / 2;
  left = Math.max(EDGE, Math.min(left, window.innerWidth - w - EDGE));
  bar.style.left = `${Math.round(left)}px`;
  bar.style.top = `${Math.round(top)}px`;
  // The pointer must survive the trip from the highlight to the strip, so the
  // burn zone spans both. Re-entering the highlight later is still a no-op:
  // only a fresh drag-select reopens the strip.
  _guard = {
    l: Math.min(target.rect.left, left) - SLACK,
    t: Math.min(target.rect.top, top) - SLACK,
    r: Math.max(target.rect.right, left + w) + SLACK,
    b: Math.max(target.rect.bottom, top + h) + SLACK,
  };
}

function copyText(text: string, btn: HTMLElement): void {
  navigator.clipboard
    .writeText(text)
    .then(() => flashDone(btn, "chat.copied"))
    .catch(() => showToast(t("chat.copyFailed"), "", "error", "Chat"));
}

function addToConversation(target: Target, btn: HTMLElement): void {
  const input = document.getElementById("prompt-input");
  if (!input) return;
  // `selection.toString()` keeps the block breaks, so a selection that starts
  // on a block boundary arrives with a leading newline. Trim BOTH ends:
  // trailing whitespace is noise, and a leading `\n` pushes the quote down by
  // a stray empty first line.
  const body = target.text.replace(/^\s+/, "").replace(/\s+$/, "");
  if (!body) return;
  // Prose goes in verbatim — no `> ` blockquote prefix (it reads as a stray
  // arrow in front of every line) — but wrapped in ONE pair of straight
  // double quotes so the pasted quote is visibly delimited. Code/terminal
  // keeps its fence instead: a `"` before the opening ``` would stop Markdown
  // from recognising the fence at all.
  const block = target.kind === "code" ? "```\n" + body + "\n```" : '"' + body + '"';
  const hasContent = (input.textContent || "").trim().length > 0;
  // An "empty" composer can still hold a leftover <br> from a line the user
  // deleted; textContent stays "" so nothing is prepended, yet the <br> still
  // renders a break and the quote lands on the second line.
  if (!hasContent) input.querySelectorAll("br").forEach((br) => br.remove());
  input.appendChild(document.createTextNode((hasContent ? "\n\n" : "") + block));
  // One synthetic input event runs the composer's own draft-save, auto-grow,
  // placeholder and send-button bookkeeping (see bindInput in app.ts).
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
  // The caret jump below changes the DOM selection, which would normally burn
  // the strip on the spot — `flashDone` locks it for the length of the beat.
  flashDone(btn, "chat.added");
  const sel = window.getSelection();
  if (!sel) return;
  const range = document.createRange();
  range.selectNodeContents(input);
  range.collapse(false);
  sel.removeAllRanges();
  sel.addRange(range);
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
