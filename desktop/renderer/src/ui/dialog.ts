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
 * Container-aware modal dialog system with priority queues.
 *
 * Every dialog is rendered inside a container element (defaulting to #app).
 * Each container maintains its own independent priority queue, so dialogs
 * from different containers can coexist (e.g. one in the settings panel and
 * one in the chat view). Within a single container only one modal dialog is
 * visible at a time; when it is dismissed the next-highest-priority dialog
 * from that container's queue is shown automatically.
 *
 * z-index values are assigned from a global monotonically-increasing
 * counter so that later dialogs always stack above earlier ones, regardless
 * of which container they live in.
 *
 * Progress dialogs use a separate overlay layer per container and do not
 * participate in the modal queue — they can coexist with a modal dialog in
 * the same container.
 *
 * swapSession() detaches all dialogs belonging to the chat-view container
 * so they can be reattached when the user switches back to that session.
 */

import { t } from "../features/i18n.js";

type Resolve<T> = (value: T) => void;

/** Priority level for modal dialogs. */
export type DialogPriority = "low" | "normal" | "high" | "critical";

const PRIORITY_RANK: Record<DialogPriority, number> = {
  low: 0,
  normal: 1,
  high: 2,
  critical: 3,
};

// ── Options ──────────────────────────────────────────────────────────────

export interface DialogOptions {
  priority?: DialogPriority;
  container?: HTMLElement;
  inline?: boolean;
}

export interface ConfirmOptions extends DialogOptions {
  primary?: string;
  secondary?: string;
}

export interface PromptOptions extends DialogOptions {
  primary?: string;
  secondary?: string;
  placeholder?: string;
  inputClass?: string;
  defaultValue?: string;
}

export interface ConfirmInstallOptions extends DialogOptions {
  primary?: string;
  secondary?: string;
  hint?: string;
}

export interface TripleConfirmOptions extends DialogOptions {
  primary?: string;
  tertiary?: string;
  secondary?: string;
}

export interface ProgressOptions {
  cancellable?: boolean;
  onCancel?: () => void;
  indeterminate?: boolean;
  container?: HTMLElement;
}

export interface ProgressHandle {
  update(progress: number, message?: string): void;
  setMessage(message: string): void;
  setSubMessage(message: string): void;
  succeed(message: string): void;
  fail(message: string): void;
  cancel(): void;
}

// ── Internal queue ──────────────────────────────────────────────────────

interface ModalEntry {
  priority: DialogPriority;
  build: () => HTMLElement;
  resolve: Resolve<any>;
  reject: (reason?: any) => void;
}

interface ContainerState {
  queue: ModalEntry[];
  currentOverlay: HTMLElement | null;
  currentResolve: Resolve<any> | null;
}

/**
 * Static collection of modal dialog builders.
 *
 * Every method returns a Promise that resolves when the user dismisses the
 * dialog. Modal dialogs are queued by priority per container and displayed
 * one at a time per container.
 */
export class Dialog {
  private static containerStates: Map<HTMLElement, ContainerState> = new Map();
  private static progressOverlays: Map<HTMLElement, HTMLElement> = new Map();
  /**
   * Global z-index counter, incremented for every overlay so later dialogs
   * always stack above earlier ones. The start value sits above every other
   * app layer — toast-overlay form dialogs (9999), the workspace manager
   * overlay (10000) and its context menu (10050), notification toasts
   * (10000) — while staying below the custom tooltip (2147483647), which is
   * a non-blocking hover aid and may legitimately appear over dialogs.
   */
  private static zCounter: number = 100000;
  private static sessionDialogStore: Map<string, HTMLElement[]> = new Map();

  // ── Container helpers ─────────────────────────────────────────────

  private static defaultContainer(): HTMLElement {
    return document.getElementById("app") || document.body;
  }

  private static resolveContainer(container?: HTMLElement): HTMLElement {
    return container || this.defaultContainer();
  }

  private static getContainerState(container: HTMLElement): ContainerState {
    let cs = this.containerStates.get(container);
    if (!cs) {
      cs = { queue: [], currentOverlay: null, currentResolve: null };
      this.containerStates.set(container, cs);
    }
    return cs;
  }

  // ── DOM helpers ─────────────────────────────────────────────────────

  private static makeOverlay(): HTMLElement {
    const overlay = document.createElement("div");
    overlay.className = "encre-dialog-overlay";
    return overlay;
  }

  private static card(overlay: HTMLElement): HTMLElement {
    const card = document.createElement("div");
    card.className = "encre-dialog-card";
    overlay.appendChild(card);
    return card;
  }

  private static titleEl(card: HTMLElement, text: string): void {
    const h = document.createElement("h3");
    h.className = "encre-dialog-title";
    h.textContent = text;
    card.appendChild(h);
  }

  private static bodyEl(card: HTMLElement, text: string): void {
    const p = document.createElement("p");
    p.className = "encre-dialog-body";
    p.textContent = text;
    card.appendChild(p);
  }

  private static buttons(card: HTMLElement, primary: string, secondary: string): {
    primaryBtn: HTMLButtonElement;
    secondaryBtn: HTMLButtonElement | null;
    footer: HTMLElement;
  } {
    const footer = document.createElement("div");
    footer.className = "encre-dialog-footer";
    card.appendChild(footer);

    let sec: HTMLButtonElement | null = null;
    if (secondary) {
      sec = document.createElement("button");
      sec.className = "btn";
      sec.textContent = secondary;
      footer.appendChild(sec);
    }

    const pri = document.createElement("button");
    pri.className = "btn btn--primary";
    pri.textContent = primary;
    footer.appendChild(pri);

    return { primaryBtn: pri, secondaryBtn: sec, footer };
  }

  /** Apply inline card styling (smaller card for narrow containers). */
  private static applyInline(overlay: HTMLElement, card: HTMLElement): void {
    card.classList.add("encre-dialog-card--inline");
  }

  // ── Queue management (per-container) ────────────────────────────────

  /**
   * Show the highest-priority entry from the container's queue, if any and
   * if no modal dialog is currently visible in that container.
   */
  private static processQueue(container: HTMLElement): void {
    const cs = this.getContainerState(container);
    if (cs.currentOverlay) return;
    if (cs.queue.length === 0) return;

    let bestIdx = 0;
    let bestRank = PRIORITY_RANK[cs.queue[0].priority];
    for (let i = 1; i < cs.queue.length; i++) {
      const r = PRIORITY_RANK[cs.queue[i].priority];
      if (r > bestRank) {
        bestRank = r;
        bestIdx = i;
      }
    }

    const entry = cs.queue.splice(bestIdx, 1)[0];
    cs.currentOverlay = entry.build();
    cs.currentResolve = entry.resolve;
    cs.currentOverlay.style.zIndex = String(++this.zCounter);
    container.appendChild(cs.currentOverlay);
  }

  /**
   * Dismiss the currently visible modal dialog in the given container
   * (remove overlay, resolve its promise, and show the next queued dialog
   * for that container).
   */
  private static dismissCurrent(container: HTMLElement, value: any): void {
    const cs = this.getContainerState(container);
    if (!cs.currentOverlay) return;
    cs.currentOverlay.remove();
    cs.currentOverlay = null;
    const resolve = cs.currentResolve;
    cs.currentResolve = null;
    if (resolve) resolve(value);
    this.processQueue(container);
  }

  /**
   * Enqueue a modal dialog into the container's queue. When it reaches the
   * front of the queue, `build` is called to create the overlay element
   * which is appended to the container. The overlay is automatically
   * removed when the dialog is dismissed.
   */
  private static enqueue<T>(
    container: HTMLElement,
    priority: DialogPriority,
    build: (resolve: Resolve<T>) => HTMLElement,
  ): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const cs = this.getContainerState(container);
      cs.queue.push({
        priority,
        build: () => build(resolve),
        resolve,
        reject,
      });
      this.processQueue(container);
    });
  }

  // ── Public API ─────────────────────────────────────────────────────

  /**
   * Shows a confirm dialog; resolves `true` for the primary button,
   * `false` otherwise.
   *
   * @param options.container - Element to mount the overlay in (must be
   *                            position:relative). Defaults to #app.
   * @param options.inline    - Use smaller card styling + click-outside-to-
   *                            dismiss (for narrow containers like sidebars).
   */
  static confirm(
    title: string,
    message: string,
    options?: ConfirmOptions,
  ): Promise<boolean> {
    const target = this.resolveContainer(options?.container);
    return this.enqueue<boolean>(target, options?.priority ?? "normal", (resolve) => {
      const overlay = this.makeOverlay();
      const card = this.card(overlay);
      if (options?.inline) this.applyInline(overlay, card);
      this.titleEl(card, title);
      this.bodyEl(card, message);
      const { primaryBtn, secondaryBtn } = this.buttons(
        card,
        options?.primary || t("dialog.confirm"),
        options?.secondary ?? t("dialog.cancel"),
      );

      const done = (val: boolean) => {
        overlay.remove();
        const cs = this.getContainerState(target);
        cs.currentOverlay = null;
        cs.currentResolve = null;
        resolve(val);
        this.processQueue(target);
      };

      primaryBtn.addEventListener("click", () => done(true));
      if (secondaryBtn) secondaryBtn.addEventListener("click", () => done(false));
      primaryBtn.focus();

      overlay.addEventListener("keydown", (e) => {
        if (e.key === "Escape") done(false);
      });
      if (options?.inline) {
        overlay.addEventListener("click", (e) => {
          if (e.target === overlay) done(false);
        });
      }

      return overlay;
    });
  }

  /**
   * Shows a confirm dialog confined to a given container (e.g. the session
   * sidebar) instead of the full screen. Convenience wrapper around
   * {@link confirm} with inline styling enabled.
   */
  static confirmIn(
    container: HTMLElement,
    title: string,
    message: string,
    options?: { primary?: string; secondary?: string },
  ): Promise<boolean> {
    return this.confirm(title, message, { ...options, container, inline: true });
  }

  /**
   * Shows a text-input prompt; resolves the entered value, or `null` on
   * cancel.
   */
  static prompt(
    title: string,
    message: string,
    options?: PromptOptions,
  ): Promise<string | null> {
    const target = this.resolveContainer(options?.container);
    return this.enqueue<string | null>(target, options?.priority ?? "normal", (resolve) => {
      const overlay = this.makeOverlay();
      const card = this.card(overlay);
      if (options?.inline) this.applyInline(overlay, card);
      this.titleEl(card, title);
      this.bodyEl(card, message);

      const input = document.createElement("input");
      input.type = "text";
      input.className = `encre-dialog-input${options?.inputClass ? ` ${options.inputClass}` : ""}`;
      input.value = options?.defaultValue ?? "";
      if (options?.placeholder) input.placeholder = options.placeholder;
      card.appendChild(input);

      const { primaryBtn, secondaryBtn } = this.buttons(
        card,
        options?.primary || t("dialog.save"),
        options?.secondary ?? t("dialog.cancel"),
      );

      const done = (val: string | null) => {
        overlay.remove();
        const cs = this.getContainerState(target);
        cs.currentOverlay = null;
        cs.currentResolve = null;
        resolve(val);
        this.processQueue(target);
      };

      primaryBtn.addEventListener("click", () => done(input.value));
      if (secondaryBtn) secondaryBtn.addEventListener("click", () => done(null));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") done(input.value);
        if (e.key === "Escape") done(null);
      });
      setTimeout(() => {
        input.focus();
        input.setSelectionRange(0, input.value.length);
      }, 100);

      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) done(null);
      });

      return overlay;
    });
  }

  /**
   * Inline prompt (inside a sidebar container, not a full-screen modal).
   * Convenience wrapper around {@link prompt} with inline styling enabled.
   */
  static promptIn(
    container: HTMLElement,
    title: string,
    message: string,
    defaultValue = "",
    options?: { primary?: string; secondary?: string; placeholder?: string; inputClass?: string },
  ): Promise<string | null> {
    return this.prompt(title, message, { ...options, defaultValue, container, inline: true });
  }

  /** Shows an informational alert with a single OK button. */
  static alert(
    title: string,
    message: string,
    options?: DialogOptions,
  ): Promise<void> {
    const target = this.resolveContainer(options?.container);
    return this.enqueue<void>(target, options?.priority ?? "normal", (resolve) => {
      const overlay = this.makeOverlay();
      const card = this.card(overlay);
      if (options?.inline) this.applyInline(overlay, card);
      this.titleEl(card, title);
      this.bodyEl(card, message);
      const { primaryBtn } = this.buttons(card, t("dialog.ok"), "");

      const done = () => {
        overlay.remove();
        const cs = this.getContainerState(target);
        cs.currentOverlay = null;
        cs.currentResolve = null;
        resolve();
        this.processQueue(target);
      };

      primaryBtn.addEventListener("click", done);
      primaryBtn.focus();
      overlay.addEventListener("keydown", (e) => {
        if (e.key === "Escape" || e.key === "Enter") done();
      });

      return overlay;
    });
  }

  // ── Engine / driver install prompt ──────────────────────────────────

  /**
   * Confirm dialog variant used for engine/driver install prompts.
   */
  static confirmInstall(
    title: string,
    body: string,
    options?: ConfirmInstallOptions,
  ): Promise<boolean> {
    const target = this.resolveContainer(options?.container);
    return this.enqueue<boolean>(target, options?.priority ?? "normal", (resolve) => {
      const overlay = this.makeOverlay();
      const card = this.card(overlay);
      if (options?.inline) this.applyInline(overlay, card);
      this.titleEl(card, title);
      this.bodyEl(card, body);
      if (options?.hint) {
        const hint = document.createElement("p");
        hint.className = "encre-dialog-hint";
        hint.textContent = options.hint;
        card.appendChild(hint);
      }
      const { primaryBtn, secondaryBtn } = this.buttons(
        card,
        options?.primary || t("dialog.confirm"),
        options?.secondary ?? t("dialog.cancel"),
      );

      const done = (val: boolean) => {
        overlay.remove();
        const cs = this.getContainerState(target);
        cs.currentOverlay = null;
        cs.currentResolve = null;
        resolve(val);
        this.processQueue(target);
      };

      primaryBtn.addEventListener("click", () => done(true));
      if (secondaryBtn) secondaryBtn.addEventListener("click", () => done(false));
      primaryBtn.focus();

      overlay.addEventListener("keydown", (e) => {
        if (e.key === "Escape") done(false);
      });
      if (options?.inline) {
        overlay.addEventListener("click", (e) => {
          if (e.target === overlay) done(false);
        });
      }

      return overlay;
    });
  }

  /**
   * Three-button confirm dialog. Resolves with the clicked button's key:
   * `"primary"`, `"secondary"`, or `"tertiary"`.
   */
  static tripleConfirm(
    title: string,
    body: string,
    options?: TripleConfirmOptions,
  ): Promise<"primary" | "tertiary" | "secondary"> {
    const target = this.resolveContainer(options?.container);
    return this.enqueue<"primary" | "tertiary" | "secondary">(
      target,
      options?.priority ?? "normal",
      (resolve) => {
        const overlay = this.makeOverlay();
        const card = this.card(overlay);
        if (options?.inline) this.applyInline(overlay, card);
        this.titleEl(card, title);
        this.bodyEl(card, body);

        const footer = document.createElement("div");
        footer.className = "encre-dialog-footer";
        card.appendChild(footer);

        let secBtn: HTMLButtonElement | null = null;
        if (options?.secondary) {
          secBtn = document.createElement("button");
          secBtn.className = "btn";
          secBtn.textContent = options.secondary;
          footer.appendChild(secBtn);
        }

        let terBtn: HTMLButtonElement | null = null;
        if (options?.tertiary) {
          terBtn = document.createElement("button");
          terBtn.className = "btn";
          terBtn.textContent = options.tertiary;
          footer.appendChild(terBtn);
        }

        const priBtn = document.createElement("button");
        priBtn.className = "btn btn--primary";
        priBtn.textContent = options?.primary || t("dialog.confirm");
        footer.appendChild(priBtn);

        const done = (choice: "primary" | "tertiary" | "secondary") => {
          overlay.remove();
          const cs = this.getContainerState(target);
          cs.currentOverlay = null;
          cs.currentResolve = null;
          resolve(choice);
          this.processQueue(target);
        };

        priBtn.addEventListener("click", () => done("primary"));
        if (secBtn) secBtn.addEventListener("click", () => done("secondary"));
        if (terBtn) terBtn.addEventListener("click", () => done("tertiary"));
        priBtn.focus();

        overlay.addEventListener("keydown", (e) => {
          if (e.key === "Escape") done("secondary");
        });
        if (options?.inline) {
          overlay.addEventListener("click", (e) => {
            if (e.target === overlay) done("secondary");
          });
        }

        return overlay;
      },
    );
  }

  // ── Progress dialog (separate layer per container) ──────────────────

  /**
   * Shows a progress dialog and returns a handle to drive it.
   *
   * Progress uses a dedicated overlay layer per container so it never
   * conflicts with modal dialogs in the same container.
   */
  static progress(
    title: string,
    initialMessage: string,
    options?: ProgressOptions,
  ): ProgressHandle {
    const target = this.resolveContainer(options?.container);

    // Remove any existing progress overlay for this container
    const existing = this.progressOverlays.get(target);
    if (existing) {
      existing.remove();
      this.progressOverlays.delete(target);
    }

    const overlay = document.createElement("div");
    overlay.className = "encre-dialog-overlay";
    overlay.style.zIndex = String(++this.zCounter);
    this.progressOverlays.set(target, overlay);
    overlay.onclick = (e) => {
      if (e.target !== overlay) return;
      if (options?.cancellable === false) return;
    };
    const card = this.card(overlay);
    card.classList.add("encre-dialog-card--progress");
    this.titleEl(card, title);

    const status = document.createElement("div");
    status.className = "encre-dialog-progress-status";
    status.textContent = initialMessage;
    card.appendChild(status);

    const track = document.createElement("div");
    track.className = "encre-dialog-progress-track";
    if (options?.indeterminate) track.classList.add("is-indeterminate");
    const fill = document.createElement("div");
    fill.className = "encre-dialog-progress-fill";
    track.appendChild(fill);
    card.appendChild(track);

    const sub = document.createElement("div");
    sub.className = "encre-dialog-progress-sub";
    sub.textContent = "";
    card.appendChild(sub);

    const pct = document.createElement("span");
    pct.className = "encre-dialog-progress-pct";
    pct.textContent = options?.indeterminate ? "" : "0%";
    status.appendChild(pct);

    let cancelBtn: HTMLButtonElement | null = null;
    if (options?.cancellable !== false) {
      const footer = document.createElement("div");
      footer.className = "encre-dialog-footer";
      card.appendChild(footer);
      cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn";
      cancelBtn.textContent = t("dialog.cancel");
      cancelBtn.addEventListener("click", () => {
        handle.cancel();
      });
      footer.appendChild(cancelBtn);
    }

    let done = false;

    const close = () => {
      overlay.classList.add("encre-dialog-dismissing");
      setTimeout(() => {
        overlay.remove();
        if (this.progressOverlays.get(target) === overlay) {
          this.progressOverlays.delete(target);
        }
      }, 150);
    };

    const setFill = (value: number) => {
      const clamped = Math.max(0, Math.min(100, value));
      fill.style.width = `${clamped}%`;
    };

    const handle: ProgressHandle = {
      update(progress: number, message?: string) {
        if (done) return;
        if (!options?.indeterminate) {
          setFill(progress);
          pct.textContent = `${Math.round(progress)}%`;
        }
        if (message !== undefined)
          status.firstChild
            ? (status.firstChild.textContent = message)
            : (status.textContent = message);
        if (status.contains(pct) && !options?.indeterminate) {
          status.appendChild(pct);
        }
      },
      setMessage(message: string) {
        if (done) return;
        const text = document.createTextNode(message);
        status.insertBefore(text, status.firstChild);
        if (status.contains(pct) && !options?.indeterminate) {
          status.appendChild(pct);
        }
      },
      setSubMessage(message: string) {
        if (done) return;
        sub.textContent = message;
      },
      succeed(message: string) {
        if (done) return;
        done = true;
        setFill(100);
        pct.textContent = "100%";
        status.firstChild
          ? (status.firstChild.textContent = message)
          : (status.textContent = message);
        if (status.contains(pct)) status.appendChild(pct);
        track.classList.add("is-success");
        if (cancelBtn) {
          cancelBtn.textContent = t("dialog.ok");
          cancelBtn.removeEventListener("click", () => handle.cancel());
          cancelBtn.addEventListener("click", close);
        } else {
          setTimeout(close, 1200);
        }
      },
      fail(message: string) {
        if (done) return;
        done = true;
        status.firstChild
          ? (status.firstChild.textContent = message)
          : (status.textContent = message);
        if (status.contains(pct)) status.appendChild(pct);
        track.classList.add("is-fail");
        if (cancelBtn) {
          cancelBtn.textContent = t("dialog.ok");
          cancelBtn.removeEventListener("click", () => handle.cancel());
          cancelBtn.addEventListener("click", close);
        } else {
          setTimeout(close, 1800);
        }
      },
      cancel() {
        if (done) return;
        done = true;
        if (options?.onCancel) options.onCancel();
        close();
      },
    };

    if (options?.indeterminate) {
      track.classList.add("is-indeterminate");
    }

    overlay.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && options?.cancellable !== false) {
        handle.cancel();
      }
    });

    target.appendChild(overlay);

    return handle;
  }

  /** Shows a dialog whose body is an arbitrary HTML element; resolves on close. */
  static showHtmlDialog(
    title: string,
    contentEl: HTMLElement,
    options?: DialogOptions,
  ): Promise<void> {
    const target = this.resolveContainer(options?.container);
    return this.enqueue<void>(target, options?.priority ?? "normal", (resolve) => {
      const overlay = this.makeOverlay();
      const card = this.card(overlay);
      if (options?.inline) this.applyInline(overlay, card);
      this.titleEl(card, title);
      card.appendChild(contentEl);

      const footer = document.createElement("div");
      footer.className = "encre-dialog-footer";
      const closeBtn = document.createElement("button");
      closeBtn.className = "btn btn--primary";
      closeBtn.textContent = t("dialog.confirm");
      footer.appendChild(closeBtn);
      card.appendChild(footer);

      const done = () => {
        overlay.remove();
        const cs = this.getContainerState(target);
        cs.currentOverlay = null;
        cs.currentResolve = null;
        resolve();
        this.processQueue(target);
      };

      closeBtn.addEventListener("click", done);
      overlay.addEventListener("keydown", (e) => {
        if (e.key === "Escape") done();
      });
      if (options?.inline) {
        overlay.addEventListener("click", (e) => {
          if (e.target === overlay) done();
        });
      }
      closeBtn.focus();

      return overlay;
    });
  }

  // ── Session-level dialog swap ──────────────────────────────────────

  /**
   * Detach all dialogs (modal + progress) belonging to the main content
   * container so they can be reattached when the user switches back to
   * that session.
   *
   * Called by Session when switching between sessions. When switching
   * from oldSid to newSid:
   *   1. All .encre-dialog-overlay children of #main-content are removed
   *      from the DOM and stored under oldSid.
   *   2. Any previously-stored dialogs for newSid are reattached.
   *
   * This makes dialogs "follow" their session: a permission prompt
   * triggered by session 2 is only visible when session 2 is active.
   */
  static swapSession(oldSid: string | null, newSid: string | null): void {
    const container = document.getElementById("main-content");
    if (!container) return;

    // Detach old session's dialogs
    if (oldSid) {
      const dialogs = Array.from(
        container.querySelectorAll<HTMLElement>(".encre-dialog-overlay"),
      );
      if (dialogs.length) {
        this.sessionDialogStore.set(oldSid, dialogs);
        dialogs.forEach((d) => d.remove());
      }
    }

    // Attach new session's dialogs
    if (newSid) {
      const stored = this.sessionDialogStore.get(newSid);
      if (stored) {
        stored.forEach((d) => container.appendChild(d));
        this.sessionDialogStore.delete(newSid);
      }
    }
  }
}
