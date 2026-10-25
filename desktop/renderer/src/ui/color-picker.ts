/**
 * The app's shared "pick a color" control — a real color picker, not a list
 * of presets.
 *
 * Same shell architecture as `ui/dropdown.ts` (`renderColorPickerImpl` +
 * `bindColorPickerImpl`, `#<id>-…` id convention, `change` event + callback),
 * but the panel is a full HSV picker drawn with pure math:
 *
 *   - an SV plane: horizontal = saturation, vertical = value (its gradient is
 *     repainted from the current hue),
 *   - a hue strip,
 *   - HEX + R/G/B numeric fields, two-way synced with the plane.
 *
 * All color conversion is standard HSV <-> RGB <-> hex algebra; there are no
 * preset swatches anywhere.  Panel anchoring reuses `placeDropdown` so the
 * panel flips exactly like a menu.
 */

import { placeDropdown } from "./dropdown.js";
import { registerSurface } from "../core/scope.js";

export const DEFAULT_COLOR = "#4f9eff";

function esc(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** `#abc` -> `#aabbcc`; returns null for anything that is not a hex color. */
export function normalizeHex(raw: string): string | null {
  const s = (raw || "").trim().replace(/^#/, "");
  if (/^[0-9a-f]{3}$/i.test(s)) return "#" + s.split("").map((c) => c + c).join("").toLowerCase();
  if (/^[0-9a-f]{6}$/i.test(s)) return "#" + s.toLowerCase();
  return null;
}

export interface Hsv {
  /** Hue in degrees, 0..360. */
  h: number;
  /** Saturation, 0..1. */
  s: number;
  /** Value (brightness), 0..1. */
  v: number;
}

const clamp01 = (x: number): number => Math.min(1, Math.max(0, x));

/** Standard hex -> HSV algebra. Returns null for non-hex input. */
export function hexToHsv(raw: string): Hsv | null {
  const hex = normalizeHex(raw);
  if (!hex) return null;
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  return { h, s: max === 0 ? 0 : d / max, v: max };
}

/** Standard HSV -> RGB (0..255 channels). */
export function hsvToRgb(h: number, s: number, v: number): { r: number; g: number; b: number } {
  const c = v * s;
  const seg = (h / 60) % 6;
  const x = c * (1 - Math.abs((seg % 2) - 1));
  let r = 0;
  let g = 0;
  let b = 0;
  if (seg < 1) { r = c; g = x; }
  else if (seg < 2) { r = x; g = c; }
  else if (seg < 3) { g = c; b = x; }
  else if (seg < 4) { g = x; b = c; }
  else if (seg < 5) { r = x; b = c; }
  else { r = c; b = x; }
  const m = v - c;
  const to255 = (t: number): number => Math.round((t + m) * 255);
  return { r: to255(r), g: to255(g), b: to255(b) };
}

export function hsvToHex(h: number, s: number, v: number): string {
  const { r, g, b } = hsvToRgb(h, s, v);
  return "#" + [r, g, b].map((t) => t.toString(16).padStart(2, "0")).join("");
}

function rgbToHex(r: number, g: number, b: number): string {
  return "#" + [r, g, b].map((t) => Math.min(255, Math.max(0, Math.round(t))).toString(16).padStart(2, "0")).join("");
}

export function renderColorPickerImpl(
  this: any,
  id: string,
  current: string,
  onChange: (hex: string) => void,
  data?: Record<string, string>,
): string {
  void onChange;
  const hex = normalizeHex(current) || DEFAULT_COLOR;
  const attrs = data
    ? Object.entries(data).map(([k, v]) => ` data-${esc(k)}="${esc(v)}"`).join("")
    : "";
  return `
    <div class="settings-color-picker-wrap" id="${id}-wrap"${attrs}>
      <button class="settings-color-picker-trigger" id="${id}-trigger" type="button">
        <span class="settings-color-picker-dot" style="background:${hex}"></span>
        <span class="settings-color-picker-value">${esc(hex)}</span>
        <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
      </button>
      <div class="settings-color-picker-panel" id="${id}-panel">
        <div class="settings-color-picker-sv" id="${id}-sv">
          <span class="settings-color-picker-cursor" id="${id}-cursor"></span>
        </div>
        <div class="settings-color-picker-hue" id="${id}-hue">
          <span class="settings-color-picker-hue-thumb" id="${id}-hue-thumb"></span>
        </div>
        <div class="settings-color-picker-rgb">
          <input class="settings-text-input settings-color-picker-hex" id="${id}-hex" value="${esc(hex)}" maxlength="7" spellcheck="false" autocomplete="off" />
          <label class="settings-color-picker-chan"><input type="number" id="${id}-r" min="0" max="255" step="1" /><span>R</span></label>
          <label class="settings-color-picker-chan"><input type="number" id="${id}-g" min="0" max="255" step="1" /><span>G</span></label>
          <label class="settings-color-picker-chan"><input type="number" id="${id}-b" min="0" max="255" step="1" /><span>B</span></label>
        </div>
      </div>
    </div>`;
}

// One capture-phase listener for the whole app: a click anywhere outside an
// open color panel closes it. Installed on the first bound picker, so a
// re-render never stacks listeners.
let outsideClickBound = false;
function bindOutsideClickClose(): void {
  if (outsideClickBound) return;
  outsideClickBound = true;
  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement | null;
    if (target?.closest(".settings-color-picker-wrap")) return;
    document.querySelectorAll(".settings-color-picker-panel.open").forEach((p) => p.classList.remove("open"));
  }, true);
}

/** Normalized drag helper: reports pointer position as 0..1 fractions of the
 *  element's box, from pointerdown through capture to release. */
function dragNormalized(el: HTMLElement, onMove: (x: number, y: number) => void): void {
  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    const report = (clientX: number, clientY: number): void => {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      onMove((clientX - r.left) / r.width, (clientY - r.top) / r.height);
    };
    report(e.clientX, e.clientY);
    const move = (ev: PointerEvent): void => report(ev.clientX, ev.clientY);
    const up = (): void => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  });
}

export function bindColorPickerImpl(this: any, id: string, onChange: (hex: string) => void): void {
  const wrap = document.getElementById(`${id}-wrap`);
  const trigger = document.getElementById(`${id}-trigger`);
  const panel = document.getElementById(`${id}-panel`);
  const sv = document.getElementById(`${id}-sv`);
  const cursor = document.getElementById(`${id}-cursor`);
  const hue = document.getElementById(`${id}-hue`);
  const hueThumb = document.getElementById(`${id}-hue-thumb`);
  const hexInput = document.getElementById(`${id}-hex`) as HTMLInputElement | null;
  const rIn = document.getElementById(`${id}-r`) as HTMLInputElement | null;
  const gIn = document.getElementById(`${id}-g`) as HTMLInputElement | null;
  const bIn = document.getElementById(`${id}-b`) as HTMLInputElement | null;
  if (!wrap || !trigger || !panel || !sv || !cursor || !hue || !hueThumb || !hexInput || !rIn || !gIn || !bIn) return;
  bindOutsideClickClose();

  const dot = trigger.querySelector<HTMLElement>(".settings-color-picker-dot");
  const valueEl = trigger.querySelector<HTMLElement>(".settings-color-picker-value");

  // Live color state; seeded from the rendered hex.
  let hsv = hexToHsv(hexInput.value) ?? { h: 217, s: 0.69, v: 1 };

  const paint = (): void => {
    const hex = hsvToHex(hsv.h, hsv.s, hsv.v);
    // The SV plane's gradient is a function of the current hue: white -> pure
    // hue horizontally, transparent -> black vertically.
    sv.style.background =
      `linear-gradient(to top, #000, rgba(0,0,0,0)), linear-gradient(to right, #fff, hsl(${hsv.h.toFixed(1)},100%,50%))`;
    cursor.style.left = `${(hsv.s * 100).toFixed(2)}%`;
    cursor.style.top = `${((1 - hsv.v) * 100).toFixed(2)}%`;
    hueThumb.style.left = `${((hsv.h / 360) * 100).toFixed(2)}%`;
    hexInput.value = hex;
    const { r, g, b } = hsvToRgb(hsv.h, hsv.s, hsv.v);
    rIn.value = String(r);
    gIn.value = String(g);
    bIn.value = String(b);
    if (dot) dot.style.background = hex;
    if (valueEl) valueEl.textContent = hex;
  };

  const emit = (): void => {
    // Same contract as the dropdown: raise `change` so every instant-save
    // ancestor stays informed, then call the owner's callback.
    wrap.dispatchEvent(new CustomEvent("change", { bubbles: true }));
    onChange(hsvToHex(hsv.h, hsv.s, hsv.v));
  };

  const setColor = (h: number, s: number, v: number, fire: boolean): void => {
    hsv = { h: Math.min(360, Math.max(0, h)), s: clamp01(s), v: clamp01(v) };
    paint();
    if (fire) emit();
  };

  // SV plane: x -> saturation, y (inverted) -> value.  Pure geometry.
  dragNormalized(sv, (x, y) => setColor(hsv.h, x, 1 - y, true));
  // Hue strip: x -> hue.
  dragNormalized(hue, (x) => setColor(x * 360, hsv.s, hsv.v, true));

  // HEX field: Enter or blur commits a valid value; an invalid one reverts.
  const commitHex = (): void => {
    const parsed = hexToHsv(hexInput.value);
    if (parsed) setColor(parsed.h, parsed.s, parsed.v, true);
    else paint();
  };
  hexInput.addEventListener("change", commitHex);
  hexInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commitHex();
    }
  });
  hexInput.addEventListener("click", (e) => e.stopPropagation());

  // R/G/B fields: commit on change; anything out of range is clamped.
  const commitChannels = (): void => {
    const r = Math.min(255, Math.max(0, Math.round(Number(rIn.value) || 0)));
    const g = Math.min(255, Math.max(0, Math.round(Number(gIn.value) || 0)));
    const b = Math.min(255, Math.max(0, Math.round(Number(bIn.value) || 0)));
    const parsed = hexToHsv(rgbToHex(r, g, b));
    if (parsed) setColor(parsed.h, parsed.s, parsed.v, true);
  };
  for (const input of [rIn, gIn, bIn]) {
    input.addEventListener("change", commitChannels);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commitChannels();
      }
    });
  }

  // Inside a panel editor the panel is still an in-wrap absolute child; a
  // scroll of the track closes it (same behaviour as the dropdown menu).
  const editorBody = wrap.closest(".panel-editor .dialog-body");
  if (editorBody) {
    editorBody.addEventListener("scroll", () => panel.classList.remove("open"), { passive: true });
  }

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = panel.classList.contains("open");
    document.querySelectorAll(".settings-color-picker-panel.open").forEach((p) => p.classList.remove("open"));
    if (!isOpen) {
      panel.classList.add("open");
      // Same ephemeral ownership as a dropdown menu: a view switch dismisses it
      // instead of leaving an open picker floating over the new conversation.
      registerSurface(panel, { scope: "ephemeral", onClose: () => panel.classList.remove("open") });
      paint();
      placeDropdown(panel, trigger);
    }
  });

  paint();
}
