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

// ---------------------------------------------------------------------------
// Dangerous-command patterns loaded from dangerous_commands.txt
// ---------------------------------------------------------------------------

/// Compiled regex table keyed by platform tag (`linux`, `windows`, `macos`).
#[derive(Default)]
struct DangerousTable {
    patterns: Vec<(String, Regex)>,
}

impl DangerousTable {
    fn load_from_file(path: &Path) -> Self {
        match fs::read_to_string(path) {
            Ok(text) => Self::load_from_text(&text),
            Err(_) => DangerousTable { patterns: Vec::new() },
        }
    }

    fn load_from_text(text: &str) -> Self {
        let mut patterns: Vec<(String, Regex)> = Vec::new();
        let mut current: Option<String> = None;
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
                }
                continue;
            }
            // Pure comment lines that look like ``# -- LINUX --...`` are
            // ignored, but inline tags like ``# [linux]`` also switch the
            // section so older formatted files keep working.
            if let Some(rest) = line.strip_prefix('#') {
                if let Some(inner) = rest.trim().strip_prefix('[') {
                    if let Some(tag) = inner.strip_suffix(']') {
                        current = Some(tag.trim().to_lowercase());
                    }
                }
                continue;
            }
            let Some(tag) = current.clone() else {
                continue;
            };
            match Regex::new(line) {
                Ok(re) => patterns.push((tag.clone(), re)),
                Err(err) => eprintln!(
                    "[encre::permission] bad regex in dangerous_commands.txt \
                     under [{tag}]: {err}"
                ),
            }
        }
        DangerousTable { patterns }
    }

    /// Test a command subcommand against the patterns for the given platform.
    /// Returns the first matching pattern string for use as a `rule` identifier.
    fn match_command(&self, command: &str, platform: &str) -> Option<String> {
        for (tag, re) in &self.patterns {
            // macOS is Unix-based: check both `macos` and `linux` tagged patterns.
            if tag != platform && !(platform == "macos" && tag == "linux") {
                continue;
            }
            if re.is_match(command) {
                return Some(re.as_str().to_string());
            }
        }
        None
    }
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
    /// Active session permission mode.  Only `"custom"` makes the user-managed
    /// per-tool / per-capability policy table effective; every other mode
    /// (manual approval, auto approval, full access) runs without it.
    mode: String,
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

/// Pure dangerous-command check for the bash tool family.  Returns the
/// matched rule string when a subcommand hits the blacklist, `None`
/// otherwise.  Used by the auto-approval classifier as a hard floor that
/// the classifier cannot override.
pub fn permission_check_dangerous(tool_name: &str, args: &str) -> Option<String> {
    let g = manager().read().expect("permission lock poisoned");
    match_dangerous(&g, tool_name, args)
}

/// Test the bash tool family input against the dangerous-pattern table,
/// splitting the command into subcommands first.
fn match_dangerous(g: &ManagerInner, tool_name: &str, args: &str) -> Option<String> {
    if !matches!(tool_name, "bash" | "bash_output") {
        return None;
    }
    let command_text = extract_bash_command(args).unwrap_or_else(|| args.to_string());
    let subcommands = split_shell_command(&command_text);
    let effective = if subcommands.is_empty() {
        vec![command_text]
    } else {
        subcommands
    };
    for cmd in effective {
        if let Some(rule) = g.dangerous.match_command(&cmd, current_platform_tag()) {
            return Some(rule);
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

    PermissionDecision::Allow
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
        assert!(tool_capabilities("todo").contains(&"misc"));
    }

    #[test]
    fn test_default_allows() {
        assert_eq!(permission_check("todo", "{}").behavior_str(), "allow");
    }

    #[test]
    fn test_deny_short_circuits() {
        let mut state = PolicyState::default();
        state
            .tools
            .insert("todo".to_string(), PolicyValue::Deny);
        permission_set_policies(state);
        match permission_check("todo", "{}") {
            PermissionDecision::Deny { .. } => {}
            other => panic!("expected deny, got {other:?}"),
        }
        // Restore default for other tests.
        permission_set_policies(PolicyState::default());
    }

}
