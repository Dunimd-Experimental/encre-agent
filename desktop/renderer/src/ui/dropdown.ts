/**
 * The app's shared "pick one" dropdown.
 *
 * Every dropdown in the desktop UI is built here: `renderDropdownImpl` returns
 * the markup (`.settings-dropdown-wrap` / `-trigger` / `.settings-dropdown` /
 * `.settings-dropdown-item`) and `bindDropdownImpl` wires it after the markup
 * is in the DOM. A feature that needs a dropdown calls these two -- it does
 * not copy the pattern, which is how the automation history panel drifted from
 * the rest of the app.
 *
 * An open menu is registered with the scope host as an EPHEMERAL surface, so
 * switching conversation or panel closes it through the same door as every
 * other popup instead of relying on a caller to remember to sweep `.open`.
 */

import { registerSurface, unregisterSurface } from "../core/scope.js";

export interface DropdownOption {
  id: string;
  label: string;
  /** Icon URL, shown before the label. */
  icon?: string;
}

function esc(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const iconImg = (src: string): string =>
  `<img class="settings-dropdown-item-icon" src="${esc(src)}" alt="" draggable="false" />`;

export function renderDropdownImpl(this: any, id: string, options: DropdownOption[], currentId: string, onChange: (val: string) => void, data?: Record<string, string>): string {
  void onChange;
  // Extra data-* attributes on the wrap (the plugin settings rows hang their
  // storage key there so bindRows can wire the widget after insertion).
  const attrs = data
    ? Object.entries(data).map(([k, v]) => ` data-${k}="${esc(v)}"`).join("")
    : "";
  const current = options.find(o => o.id === currentId) || options[0];
  const itemHtml = (o: DropdownOption) => `${o.icon ? iconImg(o.icon) : ""}${esc(o.label)}`;
  const items = options.map(o =>
    `<div class="settings-dropdown-item${o.id === currentId ? " selected" : ""}" data-value="${esc(o.id)}" data-label="${esc(o.label)}"${o.icon ? ` data-icon="${esc(o.icon)}"` : ""}>${itemHtml(o)}</div>`
  ).join("");
  if (!current) {
    return `
      <div class="settings-dropdown-wrap" id="${id}-wrap"${attrs}>
        <button class="settings-dropdown-trigger" id="${id}-trigger" type="button" disabled>
          <span>—</span>
        </button>
        <div class="settings-dropdown" id="${id}-dropdown"></div>
      </div>`;
  }
  const curHtml = current.icon ? iconImg(current.icon) : "";
  return `
    <div class="settings-dropdown-wrap" id="${id}-wrap"${attrs}>
      <button class="settings-dropdown-trigger" id="${id}-trigger" type="button">
        <span>${curHtml}${esc(current.label)}</span>
        <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
      </button>
      <div class="settings-dropdown" id="${id}-dropdown">${items}</div>
    </div>`;
}

/** Show one menu and hand it to the scope host as an ephemeral popup. */
function openMenu(menu: HTMLElement): void {
  menu.classList.add("open");
  registerSurface(menu, {
    scope: "ephemeral",
    onClose: () => menu.classList.remove("open"),
  });
}

/** Hide a menu and release it from the host. */
function closeMenu(menu: HTMLElement): void {
  unregisterSurface(menu);
  menu.classList.remove("open");
}

/** Hide every open menu (only one is ever open, but re-renders can strand one). */
function closeAllMenus(): void {
  document.querySelectorAll<HTMLElement>(".settings-dropdown.open").forEach(closeMenu);
}

// One capture-phase listener for the whole app: a click anywhere outside the
// open option list closes it. Installed on the first bound dropdown, so a
// re-render never stacks listeners.
let outsideClickBound = false;
function bindOutsideClickClose(): void {
  if (outsideClickBound) return;
  outsideClickBound = true;
  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement | null;
    if (target?.closest(".settings-dropdown-wrap")) return;
    closeAllMenus();
  }, true);
}

/** The band a menu may actually paint into: the window, narrowed by every
 *  clipping ancestor between the menu and the root.  A panel editor's dialog
 *  body is a scroll track, and the menu is an in-wrap absolute child of it --
 *  measuring the window alone would "place" the menu into a region that never
 *  gets painted, which is what made the bottom rows of the assist cards look
 *  like their dropdowns had left the app. */
function paintBand(el: HTMLElement): { top: number; bottom: number } {
  let top = 0;
  let bottom = window.innerHeight;
  let node: HTMLElement | null = el.parentElement;
  while (node && node !== document.documentElement) {
    if (getComputedStyle(node).overflowY !== "visible") {
      const r = node.getBoundingClientRect();
      if (r.top > top) top = r.top;
      if (r.bottom < bottom) bottom = r.bottom;
    }
    node = node.parentElement;
  }
  return { top, bottom };
}

/** Grow the menu downward (the app's unified anchor) unless the room under
 *  the trigger cannot hold it and the space above is larger -- then flip it to
 *  hang above the trigger.  Either way the menu's max height is clamped to the
 *  side it landed on, so a long list scrolls instead of painting off-screen.
 *  Shared with the color picker panel, which anchors exactly like a menu. */
export function placeDropdown(dropdown: HTMLElement, trigger: HTMLElement): void {
  const gap = 4;
  const edge = 8;
  dropdown.classList.remove("settings-dropdown--up");
  dropdown.style.removeProperty("--dd-max-h");

  const rect = trigger.getBoundingClientRect();
  const band = paintBand(dropdown);
  const below = band.bottom - rect.bottom - gap - edge;
  const above = rect.top - band.top - gap - edge;
  // Measured now that the menu is displayed: its natural (capped) height.
  const wanted = Math.min(320, dropdown.offsetHeight);

  if (below >= wanted || above <= below) {
    if (below < 320) dropdown.style.setProperty("--dd-max-h", `${Math.max(120, Math.floor(below))}px`);
    return;
  }
  dropdown.classList.add("settings-dropdown--up");
  dropdown.style.setProperty("--dd-max-h", `${Math.max(120, Math.floor(above))}px`);
}

export function bindDropdownImpl(this: any, id: string, onChange: (val: string) => void): void {
  const wrap = document.getElementById(`${id}-wrap`);
  const trigger = document.getElementById(`${id}-trigger`);
  const dropdown = document.getElementById(`${id}-dropdown`);
  if (!wrap || !trigger || !dropdown) return;
  if (wrap.classList.contains("is-disabled")) return;
  bindOutsideClickClose();

  // Inside a panel editor the menu is still an in-wrap absolute child (same
  // unified anchoring as everywhere else: directly BELOW the trigger, aligned
  // to it).  The scroll track just closes the menu, mirroring the menu
  // moving with its content; closing also works via item clicks and the
  // global click-outside handler (the menu stays a DOM child of the wrap).
  const editorBody = wrap.closest(".panel-editor .dialog-body");
  if (editorBody) {
    editorBody.addEventListener("scroll", () => closeMenu(dropdown), { passive: true });
  }

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = dropdown.classList.contains("open");
    // Close all other dropdowns
    closeAllMenus();
    if (!isOpen) {
      // Open first, then measure: the menu has to be displayed for its real
      // height to be known, and the flip happens in the same frame as the
      // enter animation starts, so there is no visible jump.
      openMenu(dropdown);
      placeDropdown(dropdown, trigger);
    }
  });

  dropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      const el = item as HTMLElement;
      const val = el.getAttribute("data-value") || "";
      // The option's own label, not the row's text: a row may carry markup
      // (an icon) that must not end up inside the closed trigger.
      const label = el.getAttribute("data-label") ?? el.textContent ?? "";
      const icon = el.getAttribute("data-icon") || "";
      const span = trigger.querySelector("span");
      if (span) {
        span.innerHTML = (icon ? iconImg(icon) : "") + esc(label);
      }
      closeMenu(dropdown);
      dropdown.querySelectorAll(".settings-dropdown-item").forEach((other) => other.classList.remove("selected"));
      el.classList.add("selected");
      // A native <select> raises `change`; this control only called the
      // callback, so every ancestor that listens for `change` (an instant-save
      // editor card re-snapshots its dirty baseline on it) stayed blind and
      // the card asked about changes that were already persisted.
      wrap.dispatchEvent(new CustomEvent("change", { bubbles: true }));
      onChange(val);
    });
  });
}
