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

import { t } from "../features/i18n.js";

export interface MediaData {
  type: "image" | "video";
  src: string;
  controls?: boolean;
  /** Normal mode shows the full floating toolbar (zoom/rotate/save/copy for
   *  images; play/progress/volume/save for video). Set false in special
   *  contexts (activity promo, notification center): images show no toolbar,
   *  videos show only the volume control. Default true. */
  toolbar?: boolean;
  /** Show the "open in sidebar as file preview" button (arrow-up-right icon,
   *  next to copy) on the image toolbar. Only enabled for media rendered by
   *  the info tool card. Default false. */
  openPreview?: boolean;
}

function esc(s: string): string {
  const el = document.createElement("span");
  el.textContent = s;
  return el.innerHTML;
}

function resolveUrl(src: string): string {
  const normalized = src.replace(/\\/g, "/");
  if (normalized.startsWith("local://") || normalized.startsWith("file://") || normalized.startsWith("http") || normalized.startsWith("data:")) {
    return normalized;
  }
  return "local:///" + normalized;
}

function dataUrlFromBase64(data: string, mimeType: string): string {
  const binaryStr = atob(data);
  const bytes = new Uint8Array(binaryStr.length);
  for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);
  const blob = new Blob([bytes], { type: mimeType });
  return URL.createObjectURL(blob);
}

const _allViewers: MediaViewer[] = [];

export function stopAllMedia(): void {
  _allViewers.forEach(v => v.destroy());
  _allViewers.length = 0;
}

export class MediaViewer {
  readonly el: HTMLElement;
  private media: MediaData;
  private imgEl: HTMLImageElement | null = null;
  private videoEl: HTMLVideoElement | null = null;
  private toolbarEl: HTMLElement | null = null;
  private blobUrl: string | null = null;
  private blobType = "image/png";

  // Image state
  private zoom = 1;
  private rotation = 0;
  private panX = 0;
  private panY = 0;
  private baseW = 800;
  private baseH = 500;
  private dragging = false;
  private dragStartX = 0;
  private dragStartY = 0;
  private dragPanX = 0;
  private dragPanY = 0;

  // Video state
  private progEl: HTMLElement | null = null;
  private progFillEl: HTMLElement | null = null;
  private timeEl: HTMLElement | null = null;
  private volTrackEl: HTMLElement | null = null;
  private volFillEl: HTMLElement | null = null;
  private volThumbEl: HTMLElement | null = null;
  private volIconEl: HTMLElement | null = null;
  private playBtnEl: HTMLElement | null = null;

  /** Window-level wheel handler (capture phase) so scroll-to-zoom works no
   *  matter which descendant the pointer is over. Bound in bindImageEvents,
   *  removed in destroy(). */
  private _onWheel = (e: WheelEvent): void => {
    if (this.media.type !== "image") return;
    const target = (this.imgEl ?? this.el) as HTMLElement;
    const r = target.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) return;
    if ((e.target as HTMLElement)?.closest?.(".media-viewer-toolbar")) return;
    e.preventDefault();
    e.stopPropagation();
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? r.height : 1;
    const delta = e.deltaY * unit;
    const newZoom = Math.min(5, Math.max(0.25, this.zoom * Math.exp(-delta * 0.0015)));
    const cx = r.width / 2;
    const cy = r.height / 2;
    this.panX = cx - (cx - this.panX) * newZoom / this.zoom;
    this.panY = cy - (cy - this.panY) * newZoom / this.zoom;
    this.zoom = newZoom;
    this.clampPan();
    this.applyImgTransform();
  };

  constructor(el: HTMLElement, media: MediaData) {
    this.el = el;
    this.media = media;
    _allViewers.push(this);
    this.init();
  }

  private init(): void {
    this.el.style.position = "relative";
    this.el.style.overflow = "hidden";
    this.el.classList.add("media-viewer");
    if (this.media.type === "image") {
      this.buildImage();
      this.bindImageEvents();
      this.loadMedia();
    } else if (this.media.type === "video") {
      this.buildVideo();
      this.bindVideoEvents();
      this.loadMedia();
    }
  }

  /* ── Image ─────────────────────────────────────────────────────── */

  private buildImage(): void {
    const normal = this.media.toolbar !== false;
    this.el.innerHTML =
      `<img class="media-viewer-img" alt="" />` +
      (normal
        ? `<div class="media-viewer-toolbar">` +
          `<button type="button" class="media-tool" data-action="zoom-out" data-tooltip="${t("mediaViewer.zoomOut")}"><i data-lucide="zoom-out" class="lucide" style="width:14px;height:14px"></i></button>` +
          `<button type="button" class="media-tool media-tool-label" data-action="reset" data-tooltip="${t("mediaViewer.resetZoom")}">100%</button>` +
          `<button type="button" class="media-tool" data-action="zoom-in" data-tooltip="${t("mediaViewer.zoomIn")}"><i data-lucide="zoom-in" class="lucide" style="width:14px;height:14px"></i></button>` +
          `<span class="media-tool-divider"></span>` +
          `<button type="button" class="media-tool" data-action="rotate" data-tooltip="${t("mediaViewer.rotate")}"><i data-lucide="rotate-cw" class="lucide" style="width:14px;height:14px"></i></button>` +
          `<span class="media-tool-divider"></span>` +
          `<button type="button" class="media-tool" data-action="save" data-tooltip="${t("mediaViewer.save")}"><i data-lucide="download" class="lucide" style="width:14px;height:14px"></i></button>` +
          `<button type="button" class="media-tool" data-action="copy" data-tooltip="${t("mediaViewer.copy")}"><i data-lucide="copy" class="lucide" style="width:14px;height:14px"></i></button>` +
          (this.media.openPreview
            ? `<button type="button" class="media-tool" data-action="open-preview" data-tooltip="${t("mediaViewer.openPreview")}"><i data-lucide="arrow-up-right" class="lucide" style="width:14px;height:14px"></i></button>`
            : "") +
        `</div>`
        : "");
    this.imgEl = this.el.querySelector(".media-viewer-img");
    this.toolbarEl = this.el.querySelector(".media-viewer-toolbar");
    if (this.imgEl) this.imgEl.style.transition = "none";
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.el });
    }
  }

  private bindImageEvents(): void {
    // Toolbar actions
    this.toolbarEl?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest("[data-action]") as HTMLElement | null;
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === "zoom-in") { this.setZoom(this.zoom + 0.25); this.clampPan(); }
      else if (action === "zoom-out") { this.setZoom(Math.max(0.25, this.zoom - 0.25)); this.clampPan(); }
      else if (action === "reset") { this.zoom = 1; this.rotation = 0; this.panX = 0; this.panY = 0; }
      else if (action === "rotate") this.rotation = (this.rotation + 90) % 360;
      else if (action === "save") this.saveImage();
      else if (action === "copy") this.copyImage();
      else if (action === "open-preview") this.openInSidebar();
      this.applyImgTransform();
    });

    // Pan by dragging the image (skip when interacting with toolbar)
    const stage = this.el;
    stage.addEventListener("mousedown", (e) => {
      if ((e.target as HTMLElement).closest(".media-viewer-toolbar")) return;
      if (this.zoom <= 1) return;
      this.dragging = true;
      this.dragStartX = e.clientX;
      this.dragStartY = e.clientY;
      this.dragPanX = this.panX;
      this.dragPanY = this.panY;
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!this.dragging) return;
      this.panX = this.dragPanX + (e.clientX - this.dragStartX) * 0.5;
      this.panY = this.dragPanY + (e.clientY - this.dragStartY) * 0.5;
      this.clampPan();
      this.applyImgTransform();
    });
    window.addEventListener("mouseup", () => { this.dragging = false; });

    // Scroll to zoom (centered on element). Window-level capture listener so
    // it fires regardless of which descendant is under the pointer.
    window.addEventListener("wheel", this._onWheel, { passive: false, capture: true });

    window.addEventListener("resize", () => { this.measureBase(); this.clampPan(); this.applyImgTransform(); });

    // Measure after image loads
    if (this.imgEl) {
      this.imgEl.addEventListener("load", () => { this.measureBase(); this.clampPan(); this.applyImgTransform(); });
    }
  }

  private measureBase(): void {
    if (!this.imgEl) return;
    if (this.imgEl.naturalWidth) {
      this.baseW = this.imgEl.naturalWidth;
      this.baseH = this.imgEl.naturalHeight;
    }
  }

  private clampPan(): void {
    const maxX = Math.max(0, (this.baseW * this.zoom - this.el.clientWidth) / 2);
    const maxY = Math.max(0, (this.baseH * this.zoom - this.el.clientHeight) / 2);
    this.panX = Math.max(-maxX, Math.min(maxX, this.panX));
    this.panY = Math.max(-maxY, Math.min(maxY, this.panY));
  }

  private applyImgTransform(): void {
    if (!this.imgEl) return;
    this.imgEl.style.transform = `translate(${this.panX}px, ${this.panY}px) scale(${this.zoom}) rotate(${this.rotation}deg)`;
    const label = this.toolbarEl?.querySelector<HTMLElement>('[data-action="reset"]');
    if (label) label.textContent = `${Math.round(this.zoom * 100)}%`;
  }

  private setZoom(z: number): void {
    this.zoom = Math.min(5, Math.max(0.25, z));
  }

  private saveImage(): void {
    const src = this.blobUrl || this.imgEl?.getAttribute("src") || "";
    if (!src) return;
    const a = document.createElement("a");
    a.href = src;
    a.download = this.downloadName("image", this.blobType);
    a.click();
  }

  private async copyImage(): Promise<void> {
    const src = this.blobUrl || this.imgEl?.getAttribute("src") || "";
    if (!src) return;
    try {
      const r = await fetch(src);
      const blob = await r.blob();
      const type = blob.type && blob.type !== "application/octet-stream" ? blob.type : "image/png";
      await navigator.clipboard.write([new ClipboardItem({ [type]: blob })]);
    } catch {
      if (src.startsWith("data:")) {
        navigator.clipboard.writeText(src).catch(() => {});
      }
    }
  }

  private openInSidebar(): void {
    window.dispatchEvent(new CustomEvent("media-open-preview", {
      detail: { type: this.media.type, src: this.media.src },
    }));
  }

  /* ── Video ─────────────────────────────────────────────────────── */

  private buildVideo(): void {
    // Native controls only when explicitly requested (legacy path).
    if (this.media.controls) {
      this.el.innerHTML = `<video class="media-viewer-video" controls playsinline></video>`;
      this.videoEl = this.el.querySelector(".media-viewer-video");
      return;
    }
    const normal = this.media.toolbar !== false;
    this.el.innerHTML =
      `<video class="media-viewer-video" playsinline preload="metadata"></video>` +
      (normal
        ? `<div class="media-viewer-toolbar">` +
          `<button type="button" class="media-tool" data-action="play" data-tooltip="${t("mediaViewer.play")}"><i data-lucide="play" class="lucide" style="width:14px;height:14px"></i></button>` +
          `<span class="media-tool-divider"></span>` +
          `<div class="media-progress-wrap">` +
            `<span class="media-progress-time">00:00 / 00:00</span>` +
            `<div class="media-progress" data-action="progress"><div class="media-progress-fill"></div><div class="media-progress-knob"></div></div>` +
          `</div>` +
          `<span class="media-tool-divider"></span>` +
          `<div class="media-volume">` +
            `<div class="media-volume-slider">` +
              `<div class="volume-track" data-action="volume"><div class="volume-fill"></div><div class="volume-thumb"></div></div>` +
            `</div>` +
            `<button type="button" class="media-tool media-volume-btn" data-action="vol-toggle" data-tooltip="${t("mediaViewer.volume")}"><i data-lucide="volume-2" class="lucide" style="width:14px;height:14px"></i></button>` +
          `</div>` +
          `<span class="media-tool-divider"></span>` +
          `<button type="button" class="media-tool" data-action="save" data-tooltip="${t("mediaViewer.save")}"><i data-lucide="download" class="lucide" style="width:14px;height:14px"></i></button>` +
        `</div>`
        : `<div class="media-viewer-toolbar media-viewer-toolbar--special">` +
          `<div class="media-volume">` +
            `<div class="media-volume-slider">` +
              `<div class="volume-track" data-action="volume"><div class="volume-fill"></div><div class="volume-thumb"></div></div>` +
            `</div>` +
            `<button type="button" class="media-tool media-volume-btn" data-tooltip="${t("mediaViewer.volume")}"><i data-lucide="volume-2" class="lucide" style="width:14px;height:14px"></i></button>` +
          `</div>` +
        `</div>`);
    this.videoEl = this.el.querySelector(".media-viewer-video");
    this.toolbarEl = this.el.querySelector(".media-viewer-toolbar");
    this.playBtnEl = this.el.querySelector('[data-action="play"]');
    this.progEl = this.el.querySelector(".media-progress");
    this.progFillEl = this.el.querySelector(".media-progress-fill");
    this.timeEl = this.el.querySelector(".media-progress-time");
    this.volTrackEl = this.el.querySelector(".volume-track");
    this.volFillEl = this.el.querySelector(".volume-fill");
    this.volThumbEl = this.el.querySelector(".volume-thumb");
    this.volIconEl = this.el.querySelector(".media-volume-btn i");
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.el });
    }
  }

  private bindVideoEvents(): void {
    const vid = this.videoEl;
    if (!vid) return;

    // Play / pause icon sync
    const swapPlayIcon = () => {
      if (!this.playBtnEl) return;
      const name = vid.paused ? "play" : "pause";
      this.playBtnEl.innerHTML = `<i data-lucide="${name}" class="lucide" style="width:14px;height:14px"></i>`;
      if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ nodes: [this.playBtnEl] });
    };
    vid.addEventListener("play", swapPlayIcon);
    vid.addEventListener("pause", swapPlayIcon);
    // Click video itself toggles play
    vid.addEventListener("click", () => { vid.paused ? vid.play() : vid.pause(); });

    // Progress
    const fmt = (s: number) => {
      const m = Math.floor(s / 60);
      const sec = Math.floor(s % 60);
      return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
    };
    const updateProg = () => {
      if (!vid.duration || !this.progFillEl) return;
      const pct = (vid.currentTime / vid.duration) * 100;
      this.progFillEl.style.width = `${pct}%`;
      if (this.timeEl) {
        this.timeEl.textContent = `${fmt(vid.currentTime)} / ${fmt(vid.duration)}`;
      }
    };
    vid.addEventListener("timeupdate", updateProg);
    vid.addEventListener("loadedmetadata", updateProg);
    vid.addEventListener("play", updateProg);

    this.progEl?.addEventListener("mousedown", (e) => {
      const seek = (ev: MouseEvent) => {
        if (!vid.duration || !this.progEl) return;
        const r = this.progEl.getBoundingClientRect();
        const t = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)) * vid.duration;
        vid.currentTime = t;
        updateProg();
      };
      seek(e);
      const m = (ev: MouseEvent) => seek(ev);
      const u = () => { window.removeEventListener("mousemove", m); window.removeEventListener("mouseup", u); };
      window.addEventListener("mousemove", m);
      window.addEventListener("mouseup", u);
    });

    // Volume
    const setVolume = (v: number) => {
      v = Math.max(0, Math.min(1, v));
      vid.volume = v;
      vid.muted = false;
      const pct = Math.round(v * 100);
      if (this.volFillEl) this.volFillEl.style.height = `${pct}%`;
      if (this.volThumbEl) this.volThumbEl.style.bottom = `${pct}%`;
      const name = v === 0 ? "volume-x" : v < 0.5 ? "volume-1" : "volume-2";
      if (this.volIconEl) {
        this.volIconEl.setAttribute("data-lucide", name);
        if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ nodes: [this.volIconEl] });
      }
    };
    const volFromEvent = (e: MouseEvent) => {
      if (!this.volTrackEl) return;
      const r = this.volTrackEl.getBoundingClientRect();
      setVolume(1 - (e.clientY - r.top) / r.height);
    };
    this.volTrackEl?.addEventListener("mousedown", (e) => {
      e.stopPropagation();
      volFromEvent(e);
      const m = (ev: MouseEvent) => volFromEvent(ev);
      const u = () => { window.removeEventListener("mousemove", m); window.removeEventListener("mouseup", u); };
      window.addEventListener("mousemove", m);
      window.addEventListener("mouseup", u);
    });
    // Wheel on volume adjusts volume
    const volEl = this.el.querySelector<HTMLElement>(".media-volume");
    volEl?.addEventListener("wheel", (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setVolume(vid.volume - e.deltaY * 0.002);
    }, { passive: false });

    this.toolbarEl?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest("[data-action]") as HTMLElement | null;
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === "play") { vid.paused ? vid.play() : vid.pause(); }
      else if (action === "save") { this.saveVideo(); }
    });

    // Sync toolbar with video state
    vid.addEventListener("loadedmetadata", () => setVolume(vid.volume || 0.8));
    setVolume(0.8);
  }

  private saveVideo(): void {
    const src = this.videoEl?.currentSrc || this.videoEl?.getAttribute("src") || this.blobUrl || "";
    if (!src) return;
    const a = document.createElement("a");
    a.href = src;
    a.download = this.downloadName("video", "video/mp4");
    a.click();
  }

  private downloadName(prefix: string, mimeType: string): string {
    const ext = mimeType.includes("png") ? "png" : mimeType.includes("jpeg") || mimeType.includes("jpg") ? "jpg" : mimeType.includes("webp") ? "webp" : mimeType.includes("gif") ? "gif" : mimeType.includes("mp4") ? "mp4" : mimeType.includes("webm") ? "webm" : mimeType.includes("ogg") ? "ogg" : "png";
    const base = this.media.src.replace(/^local:\/\/\//, "").replace(/^file:\/\/\//, "").split(/[/\\]/).pop() || prefix;
    const stem = base.replace(/\.[^.]+$/, "") || prefix;
    return `${stem}.${ext}`;
  }

  /* ── Shared ────────────────────────────────────────────────────── */

  private async loadMedia(): Promise<void> {
    try {
      const filePath = this.media.src.replace(/^local:\/\/\//, "").replace(/^local:\/\//, "").replace(/^file:\/\/\//, "").replace(/^file:\/\//, "");
      const result = await window.electronAPI?.readFileBase64(filePath);
      if (result) {
        this.blobType = result.mime_type || "image/png";
        this.blobUrl = dataUrlFromBase64(result.data, this.blobType);
        if (this.imgEl) this.imgEl.src = this.blobUrl;
        if (this.videoEl) {
          this.videoEl.src = this.blobUrl;
          this.videoEl.volume = 1;
          this.videoEl.muted = false;
        }
        return;
      }
    } catch {
      // IPC unavailable — fall through to direct src
    }
    const url = resolveUrl(this.media.src);
    if (this.imgEl) this.imgEl.src = url;
    if (this.videoEl) this.videoEl.src = url;
  }

  destroy(): void {
    window.removeEventListener("wheel", this._onWheel, { capture: true });
    if (this.videoEl) {
      this.videoEl.pause();
      this.videoEl.removeAttribute("src");
      this.videoEl.load();
    }
    if (this.imgEl) this.imgEl.removeAttribute("src");
    if (this.blobUrl) {
      URL.revokeObjectURL(this.blobUrl);
      this.blobUrl = null;
    }
    this.el.innerHTML = "";
  }
}

(window as any).__stopAllMedia = stopAllMedia;
