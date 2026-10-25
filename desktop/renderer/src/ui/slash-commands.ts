/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre. The Encre project belongs to the Dunimd Team.
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
 * Slash command registry.
 *
 * Defines the built-in and user/project-defined slash commands available in the
 * composer (e.g. `/plan`, `/spec`, `/new`). Commands can originate
 * from three layers — bundled, custom settings, and project-level config — and
 * are merged so that later layers override earlier ones.
 */

import { t } from "../features/i18n.js";

/** Distinguishes persistent "mode" commands from one-shot "action" commands. */
export type SlashCommandKind = "mode" | "action";

/**
 * A slash command exposed in the composer's command palette.
 */
export interface SlashCommand {
  id: string;
  name: string;
  title: string;
  description: string;
  icon: string;
  kind: SlashCommandKind;
  prompt?: string;
  source?: string;
  /** Session modes this command is available in. Empty/undefined = all modes. */
  availability?: string[];
  /**
   * Commands with a dedicated UI affordance (e.g. the new-session button)
   * stay functional but are omitted from the palette listing.
   */
  hidden?: boolean;
}

const BUILTIN_SLASH_COMMANDS: SlashCommand[] = [
  {
    id: "plan",
    name: "plan",
    title: t("slashCommands.planTitle"),
    description: t("slashCommands.planDesc"),
    // Same glyph the session review panel uses (three dots, each paired
    // with a rule) — one icon vocabulary across the app.
    icon: "list",
    kind: "mode",
  },
  {
    id: "spec",
    name: "spec",
    title: t("slashCommands.specTitle"),
    description: t("slashCommands.specDesc"),
    icon: "list",
    kind: "mode",
  },
  {
    id: "steer",
    name: "steer",
    title: t("slashCommands.steerTitle"),
    description: t("slashCommands.steerDesc"),
    icon: "navigation",
    kind: "action",
  },
  {
    id: "help",
    name: "help",
    title: "Help",
    description: "Gateway/CLI command",
    icon: "circle-help",
    kind: "action",
    // Desktop has the + menu and settings UI instead; the command stays
    // resolvable so a typed /help is answered with a hint rather than
    // forwarded to the agent.
    hidden: true,
  },
  {
    id: "model",
    name: "model",
    title: "Model",
    description: "Gateway/CLI command",
    icon: "cpu",
    kind: "action",
    // Desktop switches models through the input-toolbar selector.
    hidden: true,
  },
  {
    id: "sessions",
    name: "sessions",
    title: "Sessions",
    description: "Gateway/CLI command",
    icon: "rows-3",
    kind: "action",
    // Desktop owns sessions in the sidebar; typed input gets a hint.
    hidden: true,
  },
  {
    id: "resume",
    name: "resume",
    title: "Resume Session",
    description: "Gateway/CLI command",
    icon: "history",
    kind: "action",
    hidden: true,
  },
  {
    id: "approve",
    name: "approve",
    title: "Approve",
    description: "Gateway/CLI command",
    icon: "check-circle",
    kind: "action",
    hidden: true,
  },
  {
    id: "deny",
    name: "deny",
    title: "Deny",
    description: "Gateway/CLI command",
    icon: "x-circle",
    kind: "action",
    hidden: true,
  },
  {
    id: "stop",
    name: "stop",
    title: "Stop",
    description: "Gateway/CLI command",
    icon: "square",
    kind: "action",
    hidden: true,
  },
  {
    id: "compact",
    name: "compact",
    title: "Compact",
    description: "Gateway/CLI command",
    icon: "archive",
    kind: "action",
    hidden: true,
  },
  {
    id: "status",
    name: "status",
    title: "Status",
    description: "Gateway/CLI command",
    icon: "activity",
    kind: "action",
    hidden: true,
  },
  {
    id: "init",
    name: "init",
    title: t("slashCommands.initTitle"),
    description: t("slashCommands.initDesc"),
    icon: "wand-sparkles",
    kind: "action",
    availability: ["iwork"],
  },
];

/** Active slash commands — starts with built-in, can be overridden from backend. */
export let SLASH_COMMANDS: SlashCommand[] = [...BUILTIN_SLASH_COMMANDS];

/** Incoming command shape from the backend (both custom settings and project-level). */
export interface ServerSlashCommand {
  name: string;
  title: string;
  description: string;
  icon: string;
  kind?: string;
  prompt?: string;
  source?: string;
  availability?: string[];
}

function normalizeServerCommand(s: ServerSlashCommand): SlashCommand {
  const id = s.name;
  const kind: SlashCommandKind = s.kind === "mode" ? "mode" : "action";
  return {
    id,
    name: s.name,
    title: s.title,
    description: s.description,
    icon: "wand-2",
    kind,
    prompt: s.prompt,
    source: s.source || "user",
    availability: s.availability,
  };
}

/** Commands the desktop intentionally does not expose at all -- dedicated UI
 *  already covers them (e.g. the new-session button).  The gateway/CLI keep
 *  them; the backend registry dump must not resurrect them here. */
const DESKTOP_UNSUPPORTED = new Set(["new"]);

/** Rebuild the active slash command list from builtins, custom settings commands,
 *  and project-level commands. Each source layer overrides the previous one. */
function rebuildSlashCommands(
  customCmds: ServerSlashCommand[],
  projectCmds: ServerSlashCommand[] = [],
): void {
  const merged = new Map<string, SlashCommand>();
  for (const b of BUILTIN_SLASH_COMMANDS) {
    merged.set(b.id, { ...b, source: "bundled" });
  }
  for (const s of customCmds) {
    if (DESKTOP_UNSUPPORTED.has(s.name)) continue;
    const cmd = normalizeServerCommand(s);
    merged.set(cmd.id, cmd);
  }
  for (const s of projectCmds) {
    if (DESKTOP_UNSUPPORTED.has(s.name)) continue;
    const cmd = normalizeServerCommand(s);
    merged.set(cmd.id, cmd);
  }
  SLASH_COMMANDS = Array.from(merged.values());
}

/** Merge user-defined custom slash commands (from settings) into the active list.
 *  Custom definitions override built-ins; project-level commands are preserved. */
export function applyServerCommands(serverCmds: ServerSlashCommand[]): void {
  const project = SLASH_COMMANDS.filter((c) => c.source === "project");
  rebuildSlashCommands(serverCmds, project);
}

/** Merge project-level slash commands (from .encre/commands or .claude/commands)
 *  into the active list. Project definitions override built-ins and custom commands. */
export function applyProjectCommands(projectCmds: ServerSlashCommand[]): void {
  const custom = SLASH_COMMANDS.filter((c) => c.source !== "project" && c.source !== "bundled");
  rebuildSlashCommands(custom, projectCmds);
}

/**
 * Finds a slash command by name or id (case-insensitive, leading `/` trimmed).
 *
 * @param name - The command name or id to look up.
 * @returns The matching command, or `undefined`.
 */
export function findSlashCommand(name: string): SlashCommand | undefined {
  const normalized = name.toLowerCase().replace(/^\//, "");
  return SLASH_COMMANDS.find(
    (cmd) => cmd.name === normalized || cmd.id === normalized,
  );
}

/**
 * Parses a raw composer input string into a slash command plus trailing text.
 *
 * @param text - The raw input text.
 * @returns `{ command, rest, exact }` when it begins with a known command, else `null`.
 */
export function parseSlashInput(text: string): { command: SlashCommand; rest: string; exact: boolean } | null {
  const trimmed = text.trimStart();
  if (!trimmed.startsWith("/")) return null;

  const match = /^\/([^\s/]+)(?:\s+([\s\S]*))?$/.exec(trimmed);
  if (!match) return null;

  const command = findSlashCommand(match[1]);
  if (!command) return null;
  return {
    command,
    rest: match[2] || "",
    exact: trimmed === `/${command.name}`,
  };
}

function isAvailableInMode(cmd: SlashCommand, mode?: string): boolean {
  if (!mode) return true;
  if (!cmd.availability || cmd.availability.length === 0) return true;
  return cmd.availability.includes(mode);
}

/**
 * Returns slash commands whose name/title/description match the query.
 *
 * @param query - The (already `/`-stripped) search query; empty returns all.
 * @param mode  - Optional session mode to filter by (e.g. "iwork", "normal").
 */
export function matchingSlashCommands(query: string, mode?: string): SlashCommand[] {
  const normalized = query.toLowerCase().replace(/^\//, "");
  // Hidden commands (dedicated UI exists) never appear in the palette, yet
  // remain resolvable via findSlashCommand so typed input still triggers them.
  let cmds = SLASH_COMMANDS.filter((cmd) => !cmd.hidden);
  if (mode) cmds = cmds.filter((cmd) => isAvailableInMode(cmd, mode));
  if (!normalized) return cmds;
  return cmds.filter((cmd) =>
    cmd.name.startsWith(normalized) ||
    cmd.title.toLowerCase().includes(normalized) ||
    cmd.description.toLowerCase().includes(normalized)
  );
}

/** Whether a command matches the typed query by name (for highlight). */
export function commandMatchesQuery(cmd: SlashCommand, query: string): boolean {
  const q = query.toLowerCase().replace(/^\//, "");
  if (!q) return true;
  return cmd.name.toLowerCase().startsWith(q);
}
