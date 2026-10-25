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
 * Animated brand-logo loader component.
 *
 * A self-contained, theme-aware animated SVG loader used across the app
 * (splash screen, engine install, etc.). A single unified asset
 * (assets/Encre-load.svg) adapts its fill color via prefers-color-scheme
 * and supports a static-image mode.
 */

/**
 * EALoader - a reusable animated brand-logo loader.
 *
 * Renders the EN-W / EN-B animated SVG into the given parent element.
 * The svg is a SQUARE canvas (viewBox 102 92 820 820; the drawn "E" mark
 * is scaled up to fill it) and stretches to 100% of the parent's content
 * width, capped by `--ea-loader-max-w` (defaults to 240px).  No hard-coded
 * width / height is ever baked in, so the loader fits any container it's
 * dropped into.
 *
 * The animated loader uses a single asset whose fill color adapts to the
 * OS/app color scheme (prefers-color-scheme), so no source swapping is
 * needed for the animated mode.
 *
 * When `opts.staticSrc` is provided, the loader renders a static
 * image instead of the theme-aware animated SVGs, and skips the
 * MutationObserver for theme changes.
 */
export class EALoader {
  private readonly el: HTMLDivElement;
  private readonly img: HTMLImageElement;
  /** The full-size flex holder `mountCentered` wraps this loader in.  It
   *  must die with the loader: it is a 100%×100% box, and a stale one
   *  pushes later appends (e.g. Monaco's editor DOM, which is appended
   *  rather than innerHTML-overwritten) out of the visible container. */
  private holder: HTMLElement | null = null;
  private observer: MutationObserver | null = null;
  private isDark: boolean;
  private readonly _staticMode: boolean;
  private _showAnimated = false;
  private readonly _staticLightSrc: string | undefined;
  private readonly _staticDarkSrc: string | undefined;

  /**
   * Creates the loader inside `parent`.
   *
   * @param parent - Element to append the loader to.
   * @param opts   - Optional `maxWidth` (CSS value, capped by `--ea-loader-max-w`)
   *                 and `staticSrc` (when provided, render a static image, no observer).
   *                 When `staticSrc` is paired with `staticDarkSrc`, the loader
   *                 swaps between the two on theme change (static mode still observes).
   */
  constructor(parent: HTMLElement, opts: { maxWidth?: string; staticSrc?: string; staticDarkSrc?: string } = {}) {
    this._staticMode = !!opts.staticSrc;
    this._staticLightSrc = opts.staticSrc;
    this._staticDarkSrc = opts.staticDarkSrc;
    this.isDark = EALoader.readDark();
    this.el = document.createElement("div");
    this.el.className = "ea-loader";
    if (opts.maxWidth) {
      this.el.style.setProperty("--ea-loader-max-w", opts.maxWidth);
    }
    this.img = document.createElement("img");
    this.img.alt = "";
    this.img.draggable = false;
    this.img.decoding = "async";
    this.img.src = this.resolveSrc();
    if (opts.staticSrc) this.el.classList.add("ea-loader--static");
    this.el.appendChild(this.img);
    parent.appendChild(this.el);

    if (typeof MutationObserver !== "undefined") {
      this.observer = new MutationObserver(() => this.refresh());
      this.observer.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
    }
  }

  /** Re-read the current theme and swap the source if it flipped. */
  refresh(): void {
    const nowDark = EALoader.readDark();
    if (nowDark !== this.isDark) {
      this.isDark = nowDark;
      this.img.src = this.resolveSrc();
    }
  }

  /** Switch a static-mode loader between the still logo and the looping
   *  animated SVG. No-op for loaders created without `staticSrc` (they are
   *  already animated). */
  setAnimated(on: boolean): void {
    if (!this._staticMode) return;
    if (this._showAnimated === on) return;
    this._showAnimated = on;
    this.img.src = this.resolveSrc();
  }

  private resolveSrc(): string {
    if (this._staticMode && !this._showAnimated) {
      return this.isDark && this._staticDarkSrc ? this._staticDarkSrc : this._staticLightSrc!;
    }
    return EALoader.srcFor(this.isDark);
  }

  /**
   * Convenience factory: mounts a loader centred in `parent` inside a
   * full-size flex holder, replacing any loader previously mounted into the
   * same parent.  Use `destroyMounted` when real content takes over.
   *
   * The loader box is pinned to EXACTLY `maxWidth` wide (no stretching with
   * the container) and centred both axes -- horizontal via the holder flex,
   * vertical via `height:100%` (definite-height parents) and the box's own
   * natural height otherwise.
   */
  static mountCentered(parent: HTMLElement, maxWidth = "96px"): EALoader {
    EALoader.destroyMounted(parent);
    const host = parent as HTMLElement & { _eaLoader?: EALoader };
    const holder = document.createElement("div");
    holder.style.cssText =
      "display:flex;align-items:center;justify-content:center;width:100%;height:100%;";
    parent.appendChild(holder);
    const loader = new EALoader(holder, { maxWidth });
    // Deterministic size: the global .ea-loader CSS stretches to 100% of the
    // container width, which balloons in wide mounts even with the CSS var
    // cap.  Pin the box itself to the requested width; the inner img (100%,
    // pinned inline here so the adaptive `min(38%, ...)` rule for stretch
    // mounts cannot shrink it) then resolves to exactly that, keeping the
    // svg's aspect ratio.
    loader.el.style.cssText =
      `flex:0 0 auto;align-self:center;width:${maxWidth};height:auto;` +
      "display:flex;align-items:center;justify-content:center;";
    loader.img.style.width = "100%";
    loader.holder = holder;
    host._eaLoader = loader;
    return loader;
  }

  /** Destroys a loader previously mounted into `parent` via mountCentered. */
  static destroyMounted(parent: HTMLElement): void {
    const host = parent as HTMLElement & { _eaLoader?: EALoader };
    if (host._eaLoader) {
      host._eaLoader.destroy();
      host._eaLoader = undefined;
    }
  }

  /** Detach listeners and remove the loader from the DOM — together with
   *  its `mountCentered` holder, so the parent is left exactly as it was
   *  before the mount (no phantom full-size box stacking up). */
  destroy(): void {
    if (this.observer) {
      this.observer.disconnect();
      this.observer = null;
    }
    if (this.el.parentNode) this.el.parentNode.removeChild(this.el);
    if (this.holder) {
      if (this.holder.parentNode) this.holder.parentNode.removeChild(this.holder);
      this.holder = null;
    }
  }

  private static srcFor(_dark: boolean): string {
    return "assets/Encre-load.svg";
  }

  private static readDark(): boolean {
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr === "dark") return true;
    if (attr === "light") return false;
    try {
      return window.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch {
      return true;
    }
  }
}
