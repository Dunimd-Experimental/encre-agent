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

import { EALoader } from "../features/ealoader.js";
import { t, setLocale, type Locale } from "../features/i18n.js";
import { getState, setTheme, setThemePreference } from "../core/state.js";
import { flashCopyButton } from "../chat/chat_tool_display.js";

/** Splash defaults to English + light. The error state additionally locks
 *  the whole document into English + light so the copyable error text is
 *  always readable regardless of the user's UI language/theme setting. */
const SPLASH_LOCALE: Locale = "en";
const SPLASH_THEME = "light" as const;
/** One full loop of the animated loader SVG (see assets/Encre-load.svg). */
const LOADER_CYCLE_MS = 1600;

  /** True once the animated loader SVG has been warmed into the browser
   *  cache, so toggling the loader to its animated mode on restart is
   *  instantaneous instead of flashing blank while the SVG loads. */
let loaderPreloaded = false;

export class SplashScreen {
  private loader: EALoader | null = null;
  private errorEl: HTMLElement | null = null;
  private copyBtn: HTMLElement | null = null;
  private titleEl: HTMLElement | null = null;
  private detailEl: HTMLElement | null = null;
  private detailWrapEl: HTMLElement | null = null;
  private onRestart: (() => void) | null = null;
  private restarting = false;
  private cycleStart = 0;
  private prevLocale: Locale | null = null;
  private prevTheme: "dark" | "light" | null = null;
  private prevThemePref: "system" | "dark" | "light" | null = null;
  private locked = false;

  show(): void {
    document.body.classList.add("splash-mode");
    const el = document.getElementById("splash-screen")!;
    el.classList.remove("hidden");

    if (!loaderPreloaded) {
      loaderPreloaded = true;
      for (const src of ["assets/Encre-load.svg"]) {
        const warm = new Image();
        warm.src = src;
      }
    }

    if (!this.loader) {
      // index.html pre-paints the same still logo so the very first frame the
      // window process reveals already reads as the splash. Hand the slot to
      // the loader, which owns theme swapping from here on.
      el.querySelector(".splash-preload")?.remove();
      this.loader = new EALoader(el, {
        maxWidth: "140px",
        staticSrc: "assets/Encre-lm.svg",
        staticDarkSrc: "assets/Encre-dm.svg",
      });
    }
  }

  hide(): void {
    document.body.classList.remove("splash-mode");
    const el = document.getElementById("splash-screen");
    if (el) el.classList.add("hidden");
    if (this.loader) {
      this.loader.destroy();
      this.loader = null;
    }
    this.errorEl = null;
    this.copyBtn = null;
    this.titleEl = null;
    this.detailEl = null;
    this.detailWrapEl = null;
    this.onRestart = null;
    this.restarting = false;
    this.unlockAppearance();
  }

  showError(titleKey: string, detail: string, onRestart?: () => void): void {
    const screen = document.getElementById("splash-screen");
    if (!screen) return;

    // Lock the document into English + light BEFORE any text is resolved, so
    // t() below always renders the English copy regardless of the user's
    // saved language/theme.
    this.lockAppearance();
    // Error state shows the still logo again after a restart animation.
    this.loader?.setAnimated(false);
    this.restarting = false;

    if (!this.errorEl) {
      this.errorEl = document.createElement("div");
      this.errorEl.className = "splash-error";
      this.errorEl.innerHTML = `
        <div class="splash-error-title"></div>
        <div class="splash-error-detail-wrap">
          <div class="splash-error-detail"></div>
          <button class="btn-icon btn-icon--msg splash-error-copy-btn" data-tooltip="${t("chat.copy")}" aria-label="${t("chat.copy")}">
            <i data-lucide="copy" class="lucide lucide-sm"></i>
          </button>
        </div>
        <button class="splash-restart-btn btn btn-sm"></button>
      `;
      this.titleEl = this.errorEl.querySelector(".splash-error-title") as HTMLElement;
      this.detailWrapEl = this.errorEl.querySelector(".splash-error-detail-wrap") as HTMLElement;
      this.detailEl = this.errorEl.querySelector(".splash-error-detail") as HTMLElement;
      const btn = this.errorEl.querySelector(".splash-restart-btn") as HTMLButtonElement;
      btn.textContent = t("app.splashRestart");
      btn.onclick = () => this.triggerRestart();
      this.copyBtn = this.errorEl.querySelector(".splash-error-copy-btn") as HTMLElement;
      this.copyBtn.onclick = () => this.copyError();
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: this.errorEl });
      }
      screen.appendChild(this.errorEl);
    }

    if (this.titleEl) this.titleEl.textContent = t(titleKey);
    if (this.detailEl && this.detailWrapEl) {
      this.detailEl.textContent = detail;
      this.detailWrapEl.classList.toggle("hidden", !detail);
    }

    if (onRestart) this.onRestart = onRestart;
    if (!screen.classList.contains("splash-error-active")) {
      // Flush styles before toggling the class so the enter transition also
      // runs on the very first showError, where the error element was just
      // created in this same frame — without this the browser collapses the
      // initial and final states and the block pops in with no animation.
      void this.errorEl!.offsetHeight;
    }
    screen.classList.add("splash-error-active");
  }

  /** Begin a restart: fade the whole error block out, slide the logo back to
   *  the viewport center and switch to the looping animated loader. The
   *  caller then awaits `awaitLoaderCycle()` before judging the result so a
   *  decision never lands mid-animation. */
  beginRestart(): void {
    const screen = document.getElementById("splash-screen");
    if (!screen) return;
    this.restarting = true;
    screen.classList.remove("splash-error-active");
    this.loader?.setAnimated(true);
    this.cycleStart = performance.now();
  }

  /** Resolves at the end of the current loader loop, so success/failure
   *  transitions never cut the animation short. */
  awaitLoaderCycle(): Promise<void> {
    const elapsed = performance.now() - this.cycleStart;
    const wait = LOADER_CYCLE_MS - (elapsed % LOADER_CYCLE_MS);
    return new Promise(resolve => setTimeout(resolve, wait));
  }

  /** Copy the full error message (title + detail) to the clipboard. */
  private copyError(): void {
    const parts = [
      this.titleEl?.textContent?.trim() || "",
      this.detailEl?.textContent?.trim() || "",
    ].filter(Boolean);
    const text = parts.join("\n\n");
    if (!text || !this.copyBtn) return;
    navigator.clipboard
      .writeText(text)
      .then(() => flashCopyButton(this.copyBtn as HTMLElement))
      .catch(() => { /* clipboard denied: keep the original icon */ });
  }

  /** Restart button handler. The button is never disabled so the user can
   *  keep hammering it; only rapid-fire concurrent triggers while a single
   *  restart request is still in flight are collapsed. */
  private triggerRestart(): void {
    if (this.restarting || !this.onRestart) return;
    this.restarting = true;
    try {
      this.onRestart();
    } finally {
      this.restarting = false;
    }
  }

  /** Force English + light theme while the error state is active. This is
   *  needed because the error detail is a raw backend log string — showing
   *  it under a random UI language or a dark theme makes the payload
   *  harder to copy/paste for support. */
  private lockAppearance(): void {
    if (this.locked) return;
    this.locked = true;
    this.prevLocale = (localStorage.getItem("encre-locale") as Locale | null) || null;
    const attr = document.documentElement.getAttribute("data-theme");
    this.prevTheme = attr === "dark" || attr === "light" ? attr : null;
    try {
      this.prevThemePref = getState().themePreference;
    } catch {
      this.prevThemePref = this.prevTheme;
    }
    setLocale(SPLASH_LOCALE);
    setThemePreference(SPLASH_THEME);
    setTheme(SPLASH_THEME);
  }

  private unlockAppearance(): void {
    if (!this.locked) return;
    this.locked = false;
    if (this.prevLocale) setLocale(this.prevLocale);
    if (this.prevThemePref) setThemePreference(this.prevThemePref);
    if (this.prevTheme) setTheme(this.prevTheme);
    this.prevLocale = null;
    this.prevTheme = null;
    this.prevThemePref = null;
  }
}