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
 * Frontend side of the engine-install handshake.
 *
 * The backend raises an EngineInstallRequest event whenever a tool
 * needs a missing browser engine.  We pop a standard confirm dialog
 * (same UI as the sidebar right-click "Delete" prompt) to ask the
 * user, then switch to a canvas-style progress bar while the engine
 * downloads — all in one unified visual language.
 */

import { Dialog } from "../ui/dialog.js";
import { send } from "../core/ws.js";
import { t } from "./i18n.js";

/** A single selectable engine option presented in the install dialog. */
type EngineOption = {
  id: string;
  label: string;
  description?: string;
  kind?: "primary" | "secondary" | "danger";
  label_code?: string;
  desc_code?: string;
};

/** Backend-issued request describing which engine to install and how to present it. */
type InstallRequest = {
  request_id: string;
  engine: string;
  title: string;
  body: string;
  hint?: string;
  options?: EngineOption[];
  title_code?: string;
  title_args?: Record<string, string>;
  body_code?: string;
  body_args?: Record<string, string>;
  hint_code?: string;
  hint_args?: Record<string, string>;
};

/** Progress update emitted while an engine install is running. */
type ProgressEvent = {
  request_id: string;
  pct: number;
  message: string;
  sub_message?: string;
  indeterminate?: boolean;
  status?: "running" | "success" | "fail" | "cancelled";
  message_code?: string;
  message_args?: Record<string, string>;
  sub_message_code?: string;
  sub_message_args?: Record<string, string>;
};

/** Resolve a field via message_code, falling back to the raw string. */
function _resolveField(
  raw: string | undefined,
  code: string | undefined,
  args: Record<string, string> | undefined,
): string {
  if (code) {
    const translated = t(code, args);
    if (translated && translated !== code) return translated;
  }
  return raw || "";
}

/** Active progress handles, keyed by request_id. */
const _progressHandles: Map<string, ReturnType<typeof Dialog.progress>> = new Map();

/** Guards against re-entering the choice dialog for the same request. */
const _pendingChoices: Set<string> = new Set();

function _respond(request_id: string, choice: string): void {
  _pendingChoices.delete(request_id);
  send({
    type: "engine_install_response",
    request_id,
    choice,
    session_id: (window as any).__encreSessionId,
  } as any);
}

/** Resolve all i18n fields in an InstallRequest into a plain object. */
function _resolveReq(req: InstallRequest): { title: string; body: string; hint: string } {
  return {
    title: _resolveField(req.title, req.title_code, req.title_args),
    body: _resolveField(req.body, req.body_code, req.body_args),
    hint: _resolveField(req.hint, req.hint_code, req.hint_args),
  };
}

/** One cascaded choice — uses the same confirm UI as sidebar delete. */
async function _askOne(
  title: string,
  body: string,
  hint: string,
  opt: EngineOption,
  container?: HTMLElement,
): Promise<string> {
  const primaryLabel = _resolveField(opt.label, opt.label_code, undefined);
  const ok = await Dialog.confirmInstall(
    title,
    body,
    {
      primary: primaryLabel,
      secondary: t("engineInstall.cancel"),
      hint,
      priority: "high",
      container,
    },
  );
  if (!ok) return "cancelled";
  return opt.id;
}

/**
 * Handles an engine-install request from the backend.
 *
 * Presents a cascading choice dialog for each engine option and, once the user
 * picks one (or cancels), replies to the backend with the chosen option id.
 *
 * @param req - The install request describing engine, options and i18n fields.
 */
export async function handleEngineInstallRequest(
  req: InstallRequest,
  container?: HTMLElement,
): Promise<void> {
  if (!req || !req.request_id) return;
  if (_pendingChoices.has(req.request_id)) return;
  _pendingChoices.add(req.request_id);

  // Resolve i18n once upfront (all fields are the same across cascading rounds)
  const resolved = _resolveReq(req);

  // Resolve option labels (used for cascade text too)
  const resolveLabel = (o: EngineOption): string =>
    _resolveField(o.label, o.label_code, undefined);

  const options = (req.options && req.options.length > 0)
    ? req.options
    : [{ id: "download", label: t("engineInstall.download") }];
  let choice = "cancelled";
  try {
    for (let i = 0; i < options.length; i++) {
      const opt = options[i];
      const remaining = options.slice(i + 1).map(resolveLabel).join(" / ");
      const body = remaining
        ? `${resolved.body}\n\n${t("engineInstall.fallbackHint", { remaining })}`
        : resolved.body;
      const picked = await _askOne(resolved.title, body, resolved.hint, opt, container);
      if (picked && picked !== "cancelled") {
        choice = picked;
        break;
      }
    }
  } catch {
    choice = "cancelled";
  } finally {
    _respond(req.request_id, choice);
  }
}

/**
 * Drives a unified progress dialog (canvas-style bar).
 *
 * On the first ``running`` event the dialog opens.  Subsequent events
 * update the bar in real time.  Terminal states flip the dialog to
 * success / failure.
 */
function _progressMessage(evt: ProgressEvent): string {
  if ((evt as any).message_code) {
    const code = (evt as any).message_code;
    const translated = t(code, (evt as any).message_args);
    if (translated && translated !== code) return translated;
  }
  return evt.message || "";
}

function _progressSubMessage(evt: ProgressEvent): string {
  if ((evt as any).sub_message_code) {
    const code = (evt as any).sub_message_code;
    const translated = t(code, (evt as any).sub_message_args);
    if (translated && translated !== code) return translated;
  }
  return evt.sub_message || "";
}

/**
 * Handles a progress event for an in-flight engine install.
 *
 * Opens the unified progress dialog on the first running event and updates it
 * in real time; terminal states (success/fail/cancelled) finalize the dialog.
 *
 * @param evt - Progress event carrying percent, message and status.
 */
export function handleEngineInstallProgress(
  evt: ProgressEvent,
  container?: HTMLElement,
): void {
  if (!evt || !evt.request_id) return;
  let handle = _progressHandles.get(evt.request_id);
  const status = evt.status || "running";
  const msg = _progressMessage(evt);

  if (status === "success" || status === "fail" || status === "cancelled") {
    _pendingChoices.delete(evt.request_id);
    if (handle) {
      if (status === "success") handle.succeed(msg);
      else if (status === "fail") handle.fail(msg);
      else handle.cancel();
      setTimeout(() => _progressHandles.delete(evt.request_id), 600);
    }
    return;
  }

  if (!handle) {
    handle = Dialog.progress(
      t("engineInstall.configuring"),
      msg,
      { cancellable: true, indeterminate: evt.indeterminate !== false, container },
    );
    _progressHandles.set(evt.request_id, handle);
  }

  handle.update(evt.pct ?? 0, msg);
  const sub = _progressSubMessage(evt);
  if (sub) handle.setSubMessage(sub);
}
