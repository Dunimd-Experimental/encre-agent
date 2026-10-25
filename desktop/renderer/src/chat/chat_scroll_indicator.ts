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
 * Right-edge scroll indicator for the Chat message list.
 *
 * Renders turn "ticks" on a vertical gear-style track and lets the user
 * drag / click to jump to a specific user turn while the model streams.
 */
export class ChatScrollIndicator {
  private static readonly MAX_VISIBLE = 5;

  private container: HTMLElement;
  private root: HTMLElement;
  private track: HTMLElement;
  private thumb: HTMLElement;
  private ticks: HTMLElement[] = [];
  private tickCount = 0;
  private lastKey = "";
  private spacing = 0;
  private lastCurrent = -1;
  private dragging = false;
  private rafScheduled = false;

  constructor(container: HTMLElement) {
    this.container = container;
    this.root = document.getElementById("chat-scroll-indicator") as HTMLElement;
    this.track = document.getElementById("chat-scroll-track") as HTMLElement;
    this.thumb = document.getElementById("chat-scroll-thumb") as HTMLElement;
    this.bind();
  }

  private bind(): void {
    this.container.addEventListener("scroll", () => this.schedule(), { passive: true });
    this.root.addEventListener("mousedown", (e) => this.onPointerDown(e));
    window.addEventListener("mousemove", (e) => this.onPointerMove(e));
    window.addEventListener("mouseup", () => this.onPointerUp());
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => this.schedule());
      ro.observe(this.container);
    }
  }

  private schedule(): void {
    if (this.rafScheduled) return;
    this.rafScheduled = true;
    requestAnimationFrame(() => {
      this.rafScheduled = false;
      this.update();
    });
  }

  private rebuildTicks(count: number, texts: string[]): void {
    this.tickCount = count;
    this.spacing = 0;
    this.lastCurrent = -1;
    this.track.innerHTML = "";
    this.ticks = [];
    for (let i = 0; i < count; i++) {
      const tick = document.createElement("div");
      tick.className = "chat-scroll-tick";
      const text = (texts[i] || "").trim().slice(0, 200);
      if (text) tick.setAttribute("data-tooltip", text);
      this.track.appendChild(tick);
      this.ticks.push(tick);
    }
  }

  update(turns?: number, userTexts?: string[]): void {
    const key = `${turns}|${(userTexts || []).join("\u0000")}`;
    if (turns !== undefined && key !== this.lastKey) {
      this.rebuildTicks(turns, userTexts || []);
      this.lastKey = key;
    }
    if (this.tickCount < 1) {
      this.root.classList.add("hidden");
      return;
    }
    this.root.classList.remove("hidden");
    const { scrollTop, scrollHeight, clientHeight } = this.container;
    const scrollable = scrollHeight - clientHeight;
    if (scrollable <= 4) {
      this.root.classList.add("hidden");
      return;
    }
    const ratio = Math.min(1, Math.max(0, scrollTop / scrollable));
    // Use offsetHeight (cheap, non-layout-triggering) instead of
    // getBoundingClientRect so fast scrolling never forces a reflow.
    const windowH = this.root.offsetHeight || 70;
    const spacing = windowH / ChatScrollIndicator.MAX_VISIBLE;
    // Only re-position the ticks when the spacing actually changes;
    // otherwise their style.top/height stay fixed and we just slide
    // the track and toggle the current tick.
    if (spacing !== this.spacing) {
      this.spacing = spacing;
      for (let i = 0; i < this.tickCount; i++) {
        this.ticks[i].style.top = `${i * spacing}px`;
        this.ticks[i].style.height = `${spacing}px`;
      }
    }
    const trackH = this.tickCount * spacing;
    // Internal scroll of the track (gear metaphor): when there are more ticks
    // than fit in the window, the track slides so the current turn stays visible.
    let offset: number;
    if (trackH <= windowH) {
      offset = (windowH - trackH) / 2;
    } else {
      offset = -ratio * (trackH - windowH);
    }
    this.track.style.top = `${offset}px`;
    this.track.style.height = `${trackH}px`;
    const current = Math.round(ratio * (this.tickCount - 1));
    if (current !== this.lastCurrent) {
      if (this.lastCurrent >= 0 && this.lastCurrent < this.tickCount) {
        this.ticks[this.lastCurrent].classList.remove("current");
      }
      this.ticks[current].classList.add("current");
      this.lastCurrent = current;
    }
    this.thumb.style.top = `${current * spacing + offset + spacing / 2}px`;
  }

  /** Drops every rendered tick plus the bookkeeping that decides whether a
   *  rebuild is due, and clears the transient inline styles.
   *
   *  Callers that blank the indicator on a session / view switch MUST go
   *  through this. Emptying `.chat-scroll-track` from the outside used to
   *  leave `lastKey` / `tickCount` stale, so the next `update()` concluded
   *  the turn set was unchanged, skipped `rebuildTicks` and left the ruler
   *  blank until a new user turn happened to arrive. The `hidden` class the
   *  same reset put on the thumb was never taken back off either -- and
   *  `.hidden` is `display:none !important` -- so the progress thumb stayed
   *  invisible for the rest of the window's life.
   */
  reset(): void {
    this.lastKey = "";
    this.tickCount = 0;
    this.ticks = [];
    this.spacing = 0;
    this.lastCurrent = -1;
    this.track.innerHTML = "";
    this.track.style.top = "";
    this.track.style.height = "";
    this.thumb.classList.remove("hidden");
    this.thumb.style.top = "";
    this.root.classList.add("hidden");
  }

  private onPointerDown(e: MouseEvent): void {
    e.preventDefault();
    this.dragging = true;
    this.root.classList.add("dragging");
    this.jumpTo(e.clientY);
  }

  private onPointerMove(e: MouseEvent): void {
    if (!this.dragging) return;
    this.jumpTo(e.clientY);
  }

  private onPointerUp(): void {
    if (!this.dragging) return;
    this.dragging = false;
    this.root.classList.remove("dragging");
  }

  private jumpTo(clientY: number): void {
    const rect = this.root.getBoundingClientRect();
    if (rect.height <= 0 || this.tickCount < 1) return;
    const windowH = rect.height;
    const spacing = windowH / ChatScrollIndicator.MAX_VISIBLE;
    if (spacing !== this.spacing) this.spacing = spacing;
    const trackH = this.tickCount * spacing;
    const { scrollTop, scrollHeight, clientHeight } = this.container;
    const scrollable = scrollHeight - clientHeight;
    const ratio = scrollable > 0 ? Math.min(1, Math.max(0, scrollTop / scrollable)) : 0;
    let offset: number;
    if (trackH <= windowH) {
      offset = (windowH - trackH) / 2;
    } else {
      offset = -ratio * (trackH - windowH);
    }
    const y = Math.min(rect.bottom, Math.max(rect.top, clientY)) - rect.top;
    const idx = Math.round((y - offset) / spacing);
    const clamped = Math.max(0, Math.min(this.tickCount - 1, idx));
    this.scrollToTurn(clamped);
  }

  private scrollToTurn(idx: number): void {
    const el = this.container.querySelector<HTMLElement>(`[data-user-idx="${idx}"]`);
    if (el) {
      const cRect = this.container.getBoundingClientRect();
      const eRect = el.getBoundingClientRect();
      this.container.scrollTop += eRect.top - cRect.top;
    } else {
      const { scrollHeight, clientHeight } = this.container;
      const max = Math.max(0, scrollHeight - clientHeight);
      this.container.scrollTop = (idx / Math.max(1, this.tickCount - 1)) * max;
    }
  }
}
