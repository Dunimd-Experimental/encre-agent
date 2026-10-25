/*
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
 * Shared permission-mode catalogue and the single guarded switch path.
 *
 * Every surface that lets the user pick a permission mode (the model
 * switcher page, the automation job dialog, future plugin UIs, adapter
 * configuration, ...) renders its own UI but MUST route the actual switch
 * through `guardPermissionMode`, so the dangerous-mode gate behaves
 * identically everywhere: non-dangerous modes apply immediately, while
 * "bypass" (full access) requires an explicit danger confirmation — anything
 * else rejects the switch and keeps the previous mode.
 *
 * The confirmer is injected, which is what makes this module usable from any
 * layer: the desktop renderer passes `dialogPermissionConfirm` (the shared
 * Dialog), a backend / adapter / CLI surface passes its own prompt.  Only the
 * label lookup touches i18n; nothing here depends on the DOM.
 */

import { t } from "../features/i18n.js";
import { Dialog } from "../ui/dialog.js";

/** The four user-facing mode groups.  Legacy raw session values
 *  (accept_edits, plan, ...) collapse onto their closest card via
 *  `permissionModeGroup`. */
export type PermissionModeId = "default" | "auto" | "bypass" | "custom";

export interface PermissionModeDef {
  id: PermissionModeId;
  labelKey: string;
  /** Lucide icon name — every surface that lists modes shows these. */
  icon: string;
}

export const PERMISSION_MODES: readonly PermissionModeDef[] = [
  { id: "default", labelKey: "permissions.modes.manual", icon: "user-check" },
  { id: "auto", labelKey: "permissions.modes.auto", icon: "shield-check" },
  { id: "bypass", labelKey: "permissions.modes.full", icon: "shield-off" },
  { id: "custom", labelKey: "permissions.modes.custom", icon: "sliders-horizontal" },
];

/** Collapse any stored mode value (including legacy ones) onto one of the
 *  four user-facing groups. */
export function permissionModeGroup(m: string): PermissionModeId {
  return m === "auto" ? "auto" : m === "bypass" ? "bypass" : m === "custom" ? "custom" : "default";
}

/** Localized display name of a mode group. */
export function permissionModeLabel(m: string): string {
  const found = PERMISSION_MODES.find((x) => x.id === permissionModeGroup(m));
  return found ? t(found.labelKey) : m;
}

/** Lucide icon of a mode group — triggers / collapsed rows show the CURRENT
 *  mode's icon so the default view matches the icon-bearing mode lists. */
export function permissionModeIcon(m: string): string {
  const found = PERMISSION_MODES.find((x) => x.id === permissionModeGroup(m));
  return found ? found.icon : "user-check";
}

/** True for modes whose switch must pass the danger gate. */
export function isDangerousMode(m: string): boolean {
  return permissionModeGroup(m) === "bypass";
}

/** Payload handed to a confirmer — surface-agnostic on purpose so a
 *  non-DOM implementation can render it however it needs to. */
export interface PermissionSwitchConfirm {
  mode: PermissionModeId;
  title: string;
  body: string;
  primary: string;
}

function bypassConfirmInfo(mode: PermissionModeId): PermissionSwitchConfirm {
  return {
    mode,
    title: t("permissions.bypassConfirmTitle"),
    body: t("permissions.bypassConfirmBody"),
    primary: t("permissions.bypassConfirmPrimary"),
  };
}

/** Front-end confirmer built on the shared Dialog system.  Any DOM surface
 *  can pass this as the gate's confirmer; non-DOM surfaces inject their own. */
export async function dialogPermissionConfirm(info: PermissionSwitchConfirm): Promise<boolean> {
  return Dialog.confirm(info.title, info.body, { primary: info.primary, scope: "app" });
}

/**
 * The guarded switch path.  Returns the mode to apply, or `null` when the
 * switch is REJECTED (the user dismissed the danger warning for "bypass").
 * Non-dangerous modes pass straight through without any prompt.
 *
 * Every mode-selection UI must apply its change only when this resolves with
 * a mode — never write the mode on rejection.
 */
export async function guardPermissionMode(
  next: string,
  confirm: (info: PermissionSwitchConfirm) => Promise<boolean>,
): Promise<PermissionModeId | null> {
  const mode = permissionModeGroup(next);
  if (!isDangerousMode(mode)) return mode;
  return (await confirm(bypassConfirmInfo(mode))) ? mode : null;
}
