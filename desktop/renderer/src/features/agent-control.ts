/**
 * agent-control.ts — generic "Agent is operating this surface" isolation UI.
 *
 * This is the shared visual + state layer used whenever the Encre Agent takes
 * control of a user-facing surface (today: the embedded browser webview; in
 * the future: the computer-use tool). It renders:
 *
 *   - a soft green tint covering the whole operated surface, and
 *   - a bottom pill announcing that the Agent is operating, with a
 *     "take over" button that hands control to the user.
 *
 * The component is deliberately host-agnostic: it only owns the overlay DOM
 * and the operating/taken-over phase machine. Input isolation is a per-host
 * policy (the browser host blocks user input by mounting this overlay over
 * the webview and keeping the webview unfocused — the Agent's CDP-synthesized
 * input never goes through the UI pipeline and is unaffected; a future
 * computer-use host would enforce isolation with its own mechanism). The
 * host decides what the two callbacks mean.
 *
 * Layering note: the tint is a host-side overlay, NOT injected JavaScript
 * into the operated page — it covers every page the Agent operates without
 * per-site injection, and it cannot be dodged or altered by page scripts.
 * Keyboard/mouse isolation comes from the host policy on top of this visual.
 */
import { t, onLocaleChange } from "./i18n.js";

/** Lifecycle phase of an agent-controlled surface. */
export type AgentControlPhase = "operating" | "taken-over";

/** Handle returned by {@link createAgentControlOverlay}. */
export interface AgentControlOverlayHandle {
  /** Root overlay element; the host mounts it into the operated surface. */
  el: HTMLElement;
  /** Switch between the operating and taken-over presentations. */
  setPhase(phase: AgentControlPhase): void;
  /** Surface (or clear, with null) a pending control-change request on the
   *  pill; the user's answer is delivered through onRespond. */
  showRequest(kind: string | null, onRespond: (granted: boolean) => void): void;
  /** Current phase. */
  readonly phase: AgentControlPhase;
  /** Remove the overlay, listeners included. */
  destroy(): void;
}

export interface AgentControlOverlayOptions {
  /** User clicked "take over" — the host must release input isolation. */
  onTakeover: () => void;
  /** User clicked "hand back" — the host must re-enable input isolation. */
  onRelease: () => void;
}

/**
 * Build the isolation overlay. The caller mounts `handle.el` over the
 * operated surface (absolute inset:0 within a positioned parent).
 */
export function createAgentControlOverlay(opts: AgentControlOverlayOptions): AgentControlOverlayHandle {
  let phase: AgentControlPhase = "operating";

  const el = document.createElement("div");
  el.className = "agent-control-overlay operating";
  el.innerHTML = `
    <div class="agent-control-pill" role="status">
      <div class="agent-control-status">
        <span class="agent-control-text"></span>
        <button type="button" class="agent-control-btn btn"></button>
      </div>
      <div class="agent-control-request hidden">
        <span class="agent-control-req-text"></span>
        <button type="button" class="agent-control-agree btn"></button>
        <button type="button" class="agent-control-decline btn btn-empty"></button>
      </div>
    </div>`;

  const textEl = el.querySelector(".agent-control-text") as HTMLElement;
  const btnEl = el.querySelector(".agent-control-btn") as HTMLButtonElement;
  const statusEl = el.querySelector(".agent-control-status") as HTMLElement;
  const reqEl = el.querySelector(".agent-control-request") as HTMLElement;
  const reqTextEl = el.querySelector(".agent-control-req-text") as HTMLElement;
  const agreeEl = el.querySelector(".agent-control-agree") as HTMLButtonElement;
  const declineEl = el.querySelector(".agent-control-decline") as HTMLButtonElement;

  /** Currently displayed control-change request kind, if any. */
  let reqKind: string | null = null;
  let requestResponder: ((granted: boolean) => void) | null = null;

  const applyLocale = (): void => {
    if (phase === "operating") {
      textEl.textContent = t("agentControl.operating");
      btnEl.textContent = t("agentControl.takeover");
      btnEl.setAttribute("data-i18n-title", "agentControl.takeoverTitle");
    } else {
      textEl.textContent = t("agentControl.takenOver");
      btnEl.textContent = t("agentControl.release");
      btnEl.setAttribute("data-i18n-title", "agentControl.releaseTitle");
    }
    agreeEl.textContent = t("agentControl.agree");
    declineEl.textContent = t("agentControl.decline");
    // Only "agent_control" (asking for the wheel back) ever surfaces here —
    // handing control back concedes instantly without a pill prompt.
    if (reqKind === "agent_control") {
      reqTextEl.textContent = t("agentControl.reqAgentControl");
    }
  };
  applyLocale();
  const unsubLocale = onLocaleChange(() => applyLocale());

  btnEl.addEventListener("click", (e) => {
    e.stopPropagation();
    if (phase === "operating") {
      phase = "taken-over";
      el.classList.remove("operating");
      el.classList.add("taken-over");
      applyLocale();
      opts.onTakeover();
    } else {
      phase = "operating";
      el.classList.remove("taken-over");
      el.classList.add("operating");
      applyLocale();
      opts.onRelease();
    }
  });

  agreeEl.addEventListener("click", (e) => {
    e.stopPropagation();
    requestResponder?.(true);
  });
  declineEl.addEventListener("click", (e) => {
    e.stopPropagation();
    requestResponder?.(false);
  });

  // Swallow clicks on the tint so they never reach the operated surface
  // (belt and braces on top of the host's input-isolation policy).
  el.addEventListener("mousedown", (e) => e.stopPropagation());
  el.addEventListener("click", (e) => e.stopPropagation());

  return {
    el,
    setPhase(next: AgentControlPhase): void {
      phase = next;
      el.classList.toggle("operating", next === "operating");
      el.classList.toggle("taken-over", next === "taken-over");
      applyLocale();
    },
    /** Surface (or clear) an Agent control-change request on the pill. A
     *  request is only a REQUEST: control changes when the user answers. */
    showRequest(kind: string | null, onRespond: (granted: boolean) => void): void {
      reqKind = kind;
      requestResponder = kind ? onRespond : null;
      statusEl.classList.toggle("hidden", !!kind);
      reqEl.classList.toggle("hidden", !kind);
      if (kind) applyLocale();
    },
    get phase(): AgentControlPhase {
      return phase;
    },
    destroy(): void {
      unsubLocale();
      el.remove();
    },
  };
}
