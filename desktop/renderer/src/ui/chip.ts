/**
 * Shared helpers for composer chips (mode / command / attachment / toolbar).
 *
 * Two problems are solved here once for every chip kind:
 *
 * 1. Caret overlap — Chromium paints a collapsed caret flush against an
 *    adjacent non-editable inline element, visually overlapping the chip
 *    border.  Every chip is therefore followed by a separator space text
 *    node and the caret is placed after that space.  getPlainText() trims
 *    the stray space away on send, so serialization is unaffected.
 *
 * 2. Broken close buttons — lucide.createIcons() swaps the inner
 *    <i data-lucide class="chip-icon"> for an <svg>, which destroys any
 *    listener bound to the icon element.  Close handlers are therefore
 *    delegated onto the chip element itself (never replaced) and probe the
 *    event target with closest(".chip-icon").
 */

/** True when the node is the exact single-space separator this module inserts. */
export function isChipSeparator(node: Node | null | undefined): boolean {
  return !!node && node.nodeType === Node.TEXT_NODE && node.textContent === " ";
}

/** Removes a chip and swallows the separator space that follows it
 *  (exact single-space match only — typed prompt text is never touched). */
export function removeChipWithSeparator(chip: Element): void {
  const sep = chip.nextSibling;
  if (isChipSeparator(sep)) sep!.remove();
  chip.remove();
}

/** Moves a chip (and its separator space, if present) to the front of the
 *  composer so a newly activated chip lands before the existing draft. */
export function moveChipToFront(input: HTMLElement, chip: Element): void {
  if (input.firstChild === chip) return;
  const sep = chip.nextSibling;
  input.insertBefore(chip, input.firstChild);
  if (isChipSeparator(sep) && sep) input.insertBefore(sep, chip.nextSibling);
}

/**
 * Appends a chip to the end of the composer, inserts the separator space,
 * appends optional remaining text and places the caret right after the
 * separator — typing then continues off the chip border.
 */
export function appendChipWithCaret(input: HTMLElement, chip: HTMLElement, restText?: string): void {
  input.appendChild(chip);
  const sep = document.createTextNode(" ");
  input.appendChild(sep);
  if (restText) input.appendChild(document.createTextNode(restText));
  const sel = window.getSelection();
  if (sel) {
    const range = document.createRange();
    range.setStart(sep, 1);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
  }
}

/**
 * Inserts one or more chips at the current caret (or appends them when the
 * composer is not focused), with a separator space after the last chip so
 * the caret never sits flush against a chip border.
 */
export function insertChipsAtCaret(input: HTMLElement, chips: HTMLElement[]): void {
  const sel = window.getSelection();
  const inInput = sel && sel.rangeCount > 0 && input.contains(sel.anchorNode);
  const frag = document.createDocumentFragment();
  for (const chip of chips) frag.appendChild(chip);
  frag.appendChild(document.createTextNode(" "));
  if (inInput) {
    const range = sel!.getRangeAt(0);
    range.deleteContents();
    range.insertNode(frag);
    range.collapse(false);
    sel!.removeAllRanges();
    sel!.addRange(range);
  } else {
    input.appendChild(frag);
    const sel2 = window.getSelection();
    if (sel2) {
      const range = document.createRange();
      range.selectNodeContents(input);
      range.collapse(false);
      sel2.removeAllRanges();
      sel2.addRange(range);
    }
  }
}

/**
 * Wires the chip's close affordance (× on hover) via event delegation on the
 * chip element itself — see the module doc for why the icon element cannot
 * carry the listener.  Also covers the <img class="chip-icon"> variant.
 */
export function wireChipClose(chip: HTMLElement, onClose: () => void): void {
  chip.addEventListener("click", (e) => {
    if ((e.target as Element)?.closest?.(".chip-icon")) {
      e.stopPropagation();
      onClose();
    }
  });
}
