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

//! PyO3 bindings exposing the `encre` native core to Python as `_native`.
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::types::PyList;
use encre::diff;
use encre::embedding;
use encre::fs;
use encre::landlock;
use encre::lsp_proto;
use encre::permission;
use encre::sandbox;
use encre::search;
use encre::shell;
use encre::simd_search;
use encre::tokenizer;

// ---------------------------------------------------------------------------
// Helpers — convert Rust structs to Python dicts for zero-copy perf
// ---------------------------------------------------------------------------

fn search_results_to_py(py: Python, results: &[search::SearchResult]) -> PyResult<Vec<PyObject>> {
    let mut out = Vec::with_capacity(results.len());
    for r in results {
        let d = PyDict::new(py);

        let before_py = PyList::empty(py);
        for (ln, lc) in &r.context_before {
            let pair = PyList::empty(py);
            pair.append(ln)?;
            pair.append(lc)?;
            before_py.append(pair)?;
        }

        let after_py = PyList::empty(py);
        for (ln, lc) in &r.context_after {
            let pair = PyList::empty(py);
            pair.append(ln)?;
            pair.append(lc)?;
            after_py.append(pair)?;
        }

        d.set_item("file_path", &r.file_path)?;
        d.set_item("line_number", r.line_number)?;
        d.set_item("line_content", &r.line_content)?;
        d.set_item("score", r.score)?;
        d.set_item("context_before", before_py)?;
        d.set_item("context_after", after_py)?;
        out.push(d.into());
    }
    Ok(out)
}

fn sandbox_result_to_py(py: Python, r: &sandbox::SandboxResult) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("stdout", &r.stdout)?;
    d.set_item("stderr", &r.stderr)?;
    d.set_item("exit_code", r.exit_code)?;
    Ok(d.into())
}

/// Map a filesystem error string to the correct Python exception class.
///
/// - `"File not found: …"` → `FileNotFoundError`
/// - `"Permission denied: …"` → `PermissionError`
/// - Everything else → `RuntimeError`
fn map_fs_error(msg: String) -> PyErr {
    if msg.starts_with("File not found:") {
        PyErr::new::<pyo3::exceptions::PyFileNotFoundError, _>(msg)
    } else if msg.starts_with("Permission denied:") || msg.contains("Permission denied") {
        PyErr::new::<pyo3::exceptions::PyPermissionError, _>(msg)
    } else {
        PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(msg)
    }
}

fn shell_result_to_py(py: Python, r: &shell::ShellResult) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("stdout", &r.stdout)?;
    d.set_item("stderr", &r.stderr)?;
    d.set_item("exit_code", r.exit_code)?;
    Ok(d.into())
}

fn lsp_diagnostics_to_py(py: Python, diags: &[lsp_proto::LspDiagnostic]) -> PyResult<Vec<PyObject>> {
    let mut out = Vec::with_capacity(diags.len());
    for diag in diags {
        let d = PyDict::new(py);

        let range = PyDict::new(py);
        {
            let start = PyDict::new(py);
            start.set_item("line", diag.range.start.line)?;
            start.set_item("character", diag.range.start.character)?;
            range.set_item("start", start)?;

            let end = PyDict::new(py);
            end.set_item("line", diag.range.end.line)?;
            end.set_item("character", diag.range.end.character)?;
            range.set_item("end", end)?;
        }
        d.set_item("range", range)?;
        d.set_item("severity", diag.severity)?;
        d.set_item("message", &diag.message)?;
        d.set_item("source", &diag.source)?;
        out.push(d.into());
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Existing Python-facing functions
// ---------------------------------------------------------------------------

/// Vendor application secret injected at build time via GitHub Actions
/// (`ENCRE_EA_SECRET`).  Returns `None` in builds compiled without the
/// secret (local development), in which case Python falls back to the
/// runtime environment variable.
#[pyfunction]
fn ea_build_secret() -> Option<String> {
    option_env!("ENCRE_EA_SECRET")
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
}

#[pyfunction]
fn search_codebase(py: Python, query: &str, path: Option<&str>) -> PyResult<Vec<PyObject>> {
    let p = path.unwrap_or(".");
    let results = search::search_codebase(query, p);
    search_results_to_py(py, &results)
}

#[pyfunction]
#[pyo3(signature = (path, offset=0, limit=0))]
fn read_file(path: &str, offset: usize, limit: usize) -> PyResult<String> {
    fs::read_file(path, offset, limit)
        .map_err(|e| map_fs_error(e))
}

#[pyfunction]
fn write_file(path: &str, content: &str) -> PyResult<bool> {
    fs::write_file(path, content).map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

#[pyfunction]
#[pyo3(signature = (pattern, path, case_insensitive=false, glob_filter=None, multiline=false, head_limit=None, context_before=0, context_after=0))]
fn grep(
    py: Python,
    pattern: &str,
    path: &str,
    case_insensitive: bool,
    glob_filter: Option<&str>,
    multiline: bool,
    head_limit: Option<usize>,
    context_before: usize,
    context_after: usize,
) -> PyResult<Vec<PyObject>> {
    let mut re_builder = regex::RegexBuilder::new(pattern);
    re_builder.multi_line(true);
    if case_insensitive {
        re_builder.case_insensitive(true);
    }
    if multiline {
        re_builder.dot_matches_new_line(true);
    }
    re_builder.build().map_err(|e| {
        PyErr::new::<pyo3::exceptions::PySyntaxError, _>(format!("Invalid regex: {}", e))
    })?;

    let results = search::grep(pattern, path, case_insensitive, glob_filter, multiline, head_limit, context_before, context_after);
    search_results_to_py(py, &results)
}

#[pyfunction]
fn glob(pattern: &str, path: Option<&str>) -> PyResult<Vec<String>> {
    let p = path.unwrap_or(".");
    Ok(search::glob(pattern, p))
}

#[pyfunction]
fn count_tokens(text: &str) -> usize {
    tokenizer::count_tokens(text)
}

#[pyfunction]
fn compute_diff(old: &str, new: &str) -> String {
    diff::compute_diff(old, new)
}

#[pyfunction]
fn apply_diff(content: &str, diff_str: &str) -> PyResult<String> {
    diff::apply_diff(content, diff_str)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

#[pyfunction]
#[pyo3(signature = (command, timeout=30, workspace=None))]
fn sandbox_execute(
    py: Python,
    command: &str,
    timeout: u64,
    workspace: Option<&str>,
) -> PyResult<PyObject> {
    let result = sandbox::sandbox_execute(command, timeout, workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    sandbox_result_to_py(py, &result)
}

#[pyfunction]
fn sandbox_read_file(path: &str) -> PyResult<String> {
    sandbox::sandbox_read_file(path)
        .map_err(|e| map_fs_error(e))
}

#[pyfunction]
fn sandbox_write_file(path: &str, content: &str) -> PyResult<bool> {
    sandbox::sandbox_write_file(path, content)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

#[pyfunction]
fn execute_shell(py: Python, command: &str, cwd: Option<&str>, timeout: Option<u64>) -> PyResult<PyObject> {
    let t = timeout.unwrap_or(30);
    let result = shell::execute_shell(command, cwd, t)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    shell_result_to_py(py, &result)
}

// ---------------------------------------------------------------------------
// Similarity scoring (embedding.rs)
// ---------------------------------------------------------------------------

/// Compute cosine similarity between two f32 slices.
#[pyfunction]
fn cosine_similarity(a: Vec<f32>, b: Vec<f32>) -> f32 {
    embedding::cosine_similarity(&a, &b)
}

/// Compute Jaccard text similarity on whitespace-delimited tokens.
#[pyfunction]
fn text_similarity(a: &str, b: &str) -> f32 {
    embedding::text_similarity(a, b)
}

// ---------------------------------------------------------------------------
// New: simd_search.rs bindings
// ---------------------------------------------------------------------------

/// SIMD-accelerated substring check.
#[pyfunction]
fn simd_contains(haystack: &str, needle: &str) -> bool {
    simd_search::simd_contains(haystack, needle)
}

/// SIMD-accelerated find-all match positions (byte offsets).
#[pyfunction]
fn simd_find_all(haystack: &str, needle: &str) -> Vec<usize> {
    simd_search::simd_find_all(haystack, needle)
}

/// SIMD-accelerated byte-level memmem.
#[pyfunction]
fn simd_memmem(haystack: Vec<u8>, needle: Vec<u8>) -> Option<usize> {
    simd_search::simd_memmem(&haystack, &needle)
}

// ---------------------------------------------------------------------------
// New: landlock.rs bindings
// ---------------------------------------------------------------------------

/// Restrict the current thread to read-only filesystem access under paths.
#[pyfunction]
fn landlock_restrict_read_only(paths: Vec<String>) -> PyResult<()> {
    let refs: Vec<&str> = paths.iter().map(String::as_str).collect();
    landlock::landlock_restrict_read_only(&refs)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

/// Restrict the current thread from making network connections.
#[pyfunction]
fn landlock_restrict_network() -> PyResult<()> {
    landlock::landlock_restrict_network()
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

/// Full sandbox: read-only filesystem under workspace, no network, no exec.
#[pyfunction]
fn landlock_full_sandbox(workspace: &str) -> PyResult<()> {
    landlock::landlock_full_sandbox(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

/// Plugin worker sandbox: read-only runtime/package paths, read-write state
/// paths, no network.  Exposed for tests/diagnostics; normal plugin workers
/// use ``SandboxSession.spawn_confined`` directly.
#[pyfunction]
fn landlock_plugin_sandbox(read_paths: Vec<String>, write_paths: Vec<String>) -> PyResult<()> {
    landlock::landlock_plugin_sandbox(&read_paths, &write_paths)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
}

/// Workspace sandbox: read-write access under workspace, no network, no exec.
#[pyfunction]
fn landlock_workspace_sandbox(workspace: &str) -> PyResult<()> {
    landlock::landlock_workspace_sandbox(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

/// Check whether Landlock is available on the current kernel.
#[pyfunction]
fn landlock_available() -> bool {
    landlock::landlock_available()
}

/// Return the highest Landlock ABI version (0 if not available).
#[pyfunction]
fn landlock_abi_version() -> u64 {
    landlock::landlock_abi_version()
}

// ---------------------------------------------------------------------------
// New: lsp_proto.rs bindings
// ---------------------------------------------------------------------------

/// Parse a raw JSON-RPC 2.0 message string into a dict.
#[pyfunction]
fn parse_lsp_message(raw: &str) -> PyResult<String> {
    // Return the parsed message as a JSON string; Python can json.loads it.
    let msg = lsp_proto::parse_lsp_message(raw)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e))?;
    serde_json::to_string(&msg)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

/// Extract diagnostics from a publishDiagnostics notification params.
#[pyfunction]
fn parse_diagnostics(py: Python, raw: &str) -> PyResult<Vec<PyObject>> {
    let diags = lsp_proto::parse_diagnostics(raw)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e))?;
    lsp_diagnostics_to_py(py, &diags)
}

/// Build a JSON-RPC 2.0 request string.
#[pyfunction]
fn build_lsp_request(id: u64, method: &str, params: &str) -> PyResult<String> {
    let params_val: serde_json::Value = serde_json::from_str(params)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
    Ok(lsp_proto::build_lsp_request(id, method, params_val))
}

/// Build an LSP Content-Length header for the given body.
#[pyfunction]
fn build_content_length_header(content: &str) -> String {
    lsp_proto::build_content_length_header(content)
}

// ---------------------------------------------------------------------------
// New: permission.rs bindings
// ---------------------------------------------------------------------------

/// Run the unified permission check.  Returns a dict of the form
/// ``{"behavior": "allow"|"deny"|"ask", "reason": str?, "rule": str?}``.
#[pyfunction]
fn permission_check(py: Python, tool_name: &str, args: &str) -> PyResult<PyObject> {
    let d = permission::permission_check(tool_name, args);
    decision_to_dict(py, d)
}

/// Run the unified permission check with an explicit permission mode.
///
/// Preferred over :func:`permission_check`: the mode is passed with every
/// call, so the user-managed policy table is consulted exactly when the
/// calling session is in ``custom`` mode, regardless of what other sessions
/// pushed into the process-wide mode slot.
#[pyfunction]
fn permission_check_mode(py: Python, tool_name: &str, args: &str, mode: &str) -> PyResult<PyObject> {
    let d = permission::permission_check_mode(tool_name, args, mode);
    decision_to_dict(py, d)
}

/// Convert a Rust permission decision into the dict the Python layer expects.
fn decision_to_dict(py: Python, d: permission::PermissionDecision) -> PyResult<PyObject> {
    let out = PyDict::new(py);
    out.set_item("behavior", d.behavior_str())?;
    match d {
        permission::PermissionDecision::Allow => {}
        permission::PermissionDecision::Deny { reason } => {
            out.set_item("reason", reason)?;
        }
        permission::PermissionDecision::Ask { reason, rule } => {
            out.set_item("reason", reason)?;
            out.set_item("rule", rule)?;
        }
    }
    Ok(out.into())
}

/// Replace the active permission policy table.  ``policies_json`` is a JSON
/// string ``{"tools": {...}, "capabilities": {...}}`` where each value is
/// one of ``"allow" | "deny" | "ask" | "default"``.
#[pyfunction]
fn permission_set_policies(policies_json: &str) -> PyResult<()> {
    let state: permission::PolicyState = serde_json::from_str(policies_json)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
    permission::permission_set_policies(state);
    Ok(())
}

/// Return the active permission policy table as a JSON string.
#[pyfunction]
fn permission_get_policies() -> PyResult<String> {
    let state = permission::permission_get_policies();
    serde_json::to_string(&state)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

/// Replace the tightening-only learned-policy layer.  ``policies_json`` is a
/// JSON array of learned entries; only ``deny`` / ``ask`` entries are kept
/// (anything else, notably ``allow``, is dropped so the layer can never grant
/// access).  Learned entries are combined with the dangerous-command blacklist
/// as a hard floor and can only tighten a decision.
#[pyfunction]
fn permission_set_learned_policies(policies_json: &str) -> PyResult<()> {
    let entries: Vec<permission::LearnedPolicy> = serde_json::from_str(policies_json)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
    permission::permission_set_learned_policies(entries);
    Ok(())
}

/// Return the learned-policy layer as a JSON array.
#[pyfunction]
fn permission_get_learned_policies() -> PyResult<String> {
    let entries = permission::permission_get_learned_policies();
    serde_json::to_string(&entries)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

/// Persist the user's answer to a previous ``Ask`` decision so it doesn't
/// have to be asked again for the same tool.
#[pyfunction]
fn permission_record_decision(tool_name: &str, policy: &str) -> PyResult<()> {
    permission::permission_record_decision(tool_name, policy);
    Ok(())
}

/// Set the active session permission mode (``default`` | ``auto`` | ``bypass``
/// | ``custom`` | ...).  The user-managed policy table is only consulted in
/// ``custom`` mode; the preset modes ignore it.
#[pyfunction]
fn permission_set_mode(mode: &str) -> PyResult<()> {
    permission::permission_set_mode(mode);
    Ok(())
}

/// Replace the dangerous-command pattern table with the given rule text
/// (same format as ``dangerous_commands.txt``).  Called by Python at startup
/// so the rules always come from the package directory.
#[pyfunction]
fn permission_set_dangerous_patterns(text: &str) -> PyResult<()> {
    permission::permission_set_dangerous_patterns(text);
    Ok(())
}

/// Pure dangerous-command check for the bash tool family.  Returns the
/// matched rule string when a subcommand hits the blacklist, else ``None``.
#[pyfunction]
fn permission_check_dangerous(tool_name: &str, args: &str) -> PyResult<Option<String>> {
    Ok(permission::permission_check_dangerous(tool_name, args))
}

/// Blacklist verdict with classification: ``{"pattern", "severity",
/// "category"}`` when a subcommand hits, else ``None``.
///
/// This is what lets ``dangerous_commands.txt`` be the only definition of
/// danger -- the Python side reads severity/category off the engine instead of
/// carrying a second blacklist of its own.
#[pyfunction]
#[pyo3(signature = (tool_name, args, all_sections=false))]
fn permission_dangerous_matches(
    py: Python,
    tool_name: &str,
    args: &str,
    all_sections: bool,
) -> PyResult<Vec<Py<PyDict>>> {
    permission::permission_dangerous_matches(tool_name, args, all_sections)
        .into_iter()
        .map(|(pattern, severity, category)| {
            let d = PyDict::new(py);
            d.set_item("pattern", pattern)?;
            d.set_item("severity", severity)?;
            d.set_item("category", category)?;
            Ok(d.into())
        })
        .collect()
}

/// Blacklist verdict with classification: ``{"pattern", "severity",
/// "category"}`` for the first hit, else ``None``.
#[pyfunction]
fn permission_dangerous_verdict(
    py: Python,
    tool_name: &str,
    args: &str,
) -> PyResult<Option<Py<PyDict>>> {
    let Some((pattern, severity, category)) =
        permission::permission_dangerous_verdict(tool_name, args)
    else {
        return Ok(None);
    };
    let out = PyDict::new(py);
    out.set_item("pattern", pattern)?;
    out.set_item("severity", severity)?;
    out.set_item("category", category)?;
    Ok(Some(out.into()))
}

/// Merge dynamic per-tool danger levels for plugin tools into the permission
/// engine.  ``levels`` maps a plugin tool name to ``"safe" | "high" |
/// "critical"``; any other value is ignored.
#[pyfunction]
fn permission_set_tool_danger_levels(levels: std::collections::HashMap<String, String>) -> PyResult<()> {
    permission::permission_set_tool_danger_levels(levels);
    Ok(())
}

/// Immune-path check for one tool call.  Extracts candidate target paths
/// from the JSON ``args`` (path fields, patch markers, shell redirect / mv /
/// cp / rm / sed -i targets) and matches each against the immune set
/// (.git/.claude/.ssh/shell-init).  Only the write-tool family is inspected.
/// Returns the matched fragment when the call must be refused, else ``None``.
#[pyfunction]
fn permission_check_immune(tool_name: &str, args: &str) -> PyResult<Option<String>> {
    Ok(permission::permission_check_immune(tool_name, args))
}

/// Immune-path check for a single path string.  Returns the matched immune
/// fragment when the path is protected, else ``None``.
#[pyfunction]
fn permission_check_immune_path(path: &str) -> PyResult<Option<String>> {
    Ok(permission::permission_check_immune_path(path))
}

/// Resolve the effective user-managed policy for one capability key across
/// all configuration sources.  Returns ``"allow" | "deny" | "ask" |
/// "default"``.  Used by the plugin layer as the authorization oracle for
/// surface registration gating.
#[pyfunction]
fn permission_capability_policy(capability: &str) -> PyResult<String> {
    Ok(permission::permission_capability_policy(capability))
}

/// Register declarative permission rules for a plugin.  ``rules`` is a JSON
/// mapping ``{"<tool|prefix*>": "allow" | "deny"}``; malformed entries are
/// skipped.  Returns the number of accepted rules.  Call again with the same
/// plugin name to replace that plugin's rule set.
#[pyfunction]
fn permission_add_plugin_policy(plugin: &str, rules: &str) -> PyResult<usize> {
    Ok(permission::permission_add_plugin_policy(plugin, rules))
}

/// Remove every rule contributed by *plugin* (deactivate / uninstall).
/// Returns the number of rules dropped.
#[pyfunction]
fn permission_remove_plugin_policies(plugin: &str) -> PyResult<usize> {
    Ok(permission::permission_remove_plugin_policies(plugin))
}

/// Plugin-policy verdict for one tool, computed inside the engine: deny
/// beats allow, exact rule beats wildcard.  Returns ``True`` / ``False`` /
/// ``None`` (no rule applies).
#[pyfunction]
fn permission_plugin_vote(tool_name: &str) -> PyResult<Option<bool>> {
    Ok(permission::permission_plugin_vote(tool_name))
}

/// Dump the current plugin policy table as a JSON object
/// ``{"<plugin>": {"<tool|*>": "allow" | "deny"}}``.
#[pyfunction]
fn permission_get_plugin_policies() -> PyResult<String> {
    Ok(permission::permission_get_plugin_policies())
}

/// Register the manifest-declared resource grants of a plugin (replaces any
/// previous grants).  ``resources_json`` shape:
/// ``{"network": [hosts], "file_read": [globs], "file_write": [globs],
/// "system_info": true | false | [keys]}``.  Returns the number of
/// recognised kinds.
#[pyfunction]
fn permission_set_plugin_resources(plugin: &str, resources_json: &str) -> PyResult<usize> {
    Ok(permission::permission_set_plugin_resources(
        plugin,
        resources_json,
    ))
}

/// Drop the resource grants of *plugin* (deactivate / uninstall).  Returns
/// true when an entry existed.
#[pyfunction]
fn permission_remove_plugin_resources(plugin: &str) -> PyResult<bool> {
    Ok(permission::permission_remove_plugin_resources(plugin))
}

/// Arbitrate one resource access for *plugin*: kind is ``"network"`` (URL /
/// host), ``"file_read"`` / ``"file_write"`` (path) or ``"system_info"``
/// (key).  Returns ``"allow"`` or ``"deny"``; unknown kinds and unknown
/// plugins always deny (fail-closed).
#[pyfunction]
fn permission_check_plugin_resource(plugin: &str, kind: &str, target: &str) -> PyResult<String> {
    Ok(permission::permission_check_plugin_resource(
        plugin, kind, target,
    ))
}

/// Dump the current resource grant table as a JSON object
/// ``{"<plugin>": {"network": [...], "file_read": [...], "file_write":
/// [...], "system_info": true | [keys] | false}}``.
#[pyfunction]
fn permission_get_plugin_resources() -> PyResult<String> {
    Ok(permission::permission_get_plugin_resources())
}

/// Record *tool* as owned by *plugin* and mark it active.  Returns true on
/// success (both names non-empty).
#[pyfunction]
fn permission_register_plugin_tool(tool: &str, plugin: &str) -> PyResult<bool> {
    Ok(permission::permission_register_plugin_tool(tool, plugin))
}

/// Flip every tool owned by *plugin* to *active* (deactivate = false,
/// re-activate = true).  Returns the number of affected tools.
#[pyfunction]
fn permission_set_plugin_tools_active(plugin: &str, active: bool) -> PyResult<usize> {
    Ok(permission::permission_set_plugin_tools_active(plugin, active))
}

/// Return the owning plugin of *tool* (``""`` = host tool or unknown).
#[pyfunction]
fn permission_plugin_tool_owner(tool: &str) -> PyResult<String> {
    Ok(permission::permission_plugin_tool_owner(tool))
}

/// Record the frontend surface grants for *plugin* (trusted = bundled /
/// mandatory: unset fields default to allow; third-party defaults to deny).
#[pyfunction]
fn permission_set_plugin_ui(plugin: &str, payload: &str, trusted: bool) -> PyResult<bool> {
    Ok(permission::permission_set_plugin_ui(plugin, payload, trusted))
}

/// Remove the frontend grants of *plugin* (deactivate / uninstall undo).
#[pyfunction]
fn permission_remove_plugin_ui(plugin: &str) -> PyResult<bool> {
    Ok(permission::permission_remove_plugin_ui(plugin))
}

/// Arbitrate one frontend surface: face = "providers" | "tool_meta" |
/// "contribution" (kind = contribution kind).  Fail-closed.
#[pyfunction]
fn permission_check_plugin_ui(plugin: &str, face: &str, kind: &str) -> PyResult<bool> {
    Ok(permission::permission_check_plugin_ui(plugin, face, kind))
}

/// Build the sanitised environment for a plugin worker process.  Sensitive
/// variables (TOKEN/KEY/SECRET/PASSWORD/CREDENTIAL) are stripped from the
/// parent environment; *extra* entries are applied last and unfiltered.
#[pyfunction]
fn plugin_sandbox_prepare_env(
    extra: std::collections::HashMap<String, String>,
) -> PyResult<std::collections::HashMap<String, String>> {
    Ok(encre::plugin_sandbox::plugin_sandbox_prepare_env(extra))
}

/// Resolve manifest resource limits into concrete per-OS values.
///
/// Returns ``(memory_bytes, cpu_rate, cpu_fraction)``: the Job memory cap /
/// POSIX ``RLIMIT_AS`` value (0 = unlimited), the Windows CPU rate
/// (1..=10000, 0 = unlimited) and the normalised core fraction for the
/// software watchdog (0.0 = unlimited).
#[pyfunction]
fn plugin_sandbox_resolve_limits(
    memory_mb: Option<f64>,
    cpu: Option<f64>,
) -> PyResult<(u64, u64, f64)> {
    let limits = encre::plugin_sandbox::plugin_sandbox_resolve_limits(memory_mb, cpu);
    Ok((limits.memory_bytes, limits.cpu_rate, limits.cpu_fraction))
}

// ---------------------------------------------------------------------------
// SandboxSession — persistent sandboxed subprocess (confinement owned by Rust)
// ---------------------------------------------------------------------------

/// A long-lived sandboxed child process with piped stdio.
///
/// All OS-level confinement (Landlock on Linux, sandbox_init(3) on macOS,
/// a kill-on-close Job Object on Windows) is applied INSIDE this Rust type at
/// spawn time.  Python only drives the pipes; there is no privilege decision
/// left on the Python side for a child to bypass.
#[pyclass]
struct PySandboxSession {
    /// Mutex so blocking waits can run with the GIL released.
    inner: std::sync::Mutex<sandbox::SandboxSession>,
}

#[pymethods]
impl PySandboxSession {
    /// Spawn *argv* under the platform sandbox.
    ///
    /// Returns ``(session, isolation_level)`` where the level is one of
    /// ``"landlock"``, ``"sandbox_init"``, ``"job_object"`` or ``"none"``
    /// (reported honestly: spawn never claims isolation it did not get).
    #[staticmethod]
    fn spawn(
        argv: Vec<String>,
        cwd: Option<String>,
        workspace: Option<String>,
    ) -> PyResult<(PySandboxSession, String)> {
        let (session, level) = sandbox::SandboxSession::spawn(
            &argv,
            cwd.as_deref(),
            workspace.as_deref(),
        )
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
        Ok((
            PySandboxSession { inner: std::sync::Mutex::new(session) },
            level.to_string(),
        ))
    }

    /// Spawn *argv* under the platform sandbox with an explicit environment
    /// and a profile:
    ///   - `workspace` -> legacy workspace confinement;
    ///   - non-empty `read_paths` / `write_paths` -> plugin worker profile
    ///     (read-only runtime/package, read-write state, no network);
    ///   - all empty -> no filesystem confinement.
    #[staticmethod]
    fn spawn_confined(
        argv: Vec<String>,
        cwd: Option<String>,
        env: std::collections::HashMap<String, String>,
        workspace: Option<String>,
        read_paths: Vec<String>,
        write_paths: Vec<String>,
    ) -> PyResult<(PySandboxSession, String)> {
        let profile = if !read_paths.is_empty() || !write_paths.is_empty() {
            encre::sandbox::SandboxProfile::Plugin { read_paths, write_paths }
        } else if let Some(ws) = workspace {
            encre::sandbox::SandboxProfile::Workspace(ws)
        } else {
            encre::sandbox::SandboxProfile::None
        };
        let (session, level) = sandbox::SandboxSession::spawn_with_env(
            &argv,
            cwd.as_deref(),
            Some(&env),
            profile,
        )
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
        Ok((
            PySandboxSession { inner: std::sync::Mutex::new(session) },
            level.to_string(),
        ))
    }

    /// The honest isolation level reported at spawn time.
    fn isolation_level(&self) -> String {
        match self.inner.lock() {
            Ok(s) => s.isolation_level().to_string(),
            Err(_) => "none".to_string(),
        }
    }

    /// The confined child's OS process id.
    fn pid(&self) -> PyResult<u32> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("session poisoned"))?
            .pid())
    }

    /// Write bytes to the child's stdin; returns the byte count written.
    fn write_stdin(&self, data: Vec<u8>) -> PyResult<usize> {
        self.inner
            .lock()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("session poisoned"))?
            .write_stdin(&data)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
    }

    /// Close the child's stdin (end-of-input for shells).
    fn close_stdin(&self) -> PyResult<()> {
        self.inner
            .lock()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("session poisoned"))?
            .close_stdin();
        Ok(())
    }

    /// Drain pending stdout bytes (waits up to *timeout_ms* for data).
    /// Blocks with the GIL released, so other threads keep running.
    fn drain_stdout<'py>(
        &self,
        py: Python<'py>,
        max_bytes: usize,
        timeout_ms: u64,
    ) -> PyResult<Bound<'py, pyo3::types::PyBytes>> {
        let bytes = py.allow_threads(|| {
            self.inner
                .lock()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("session poisoned"))
                .map(|s| s.drain_stdout(max_bytes, timeout_ms))
        })?;
        Ok(pyo3::types::PyBytes::new(py, &bytes))
    }

    /// Drain pending stderr bytes (waits up to *timeout_ms* for data).
    /// Blocks with the GIL released, so other threads keep running.
    fn drain_stderr<'py>(
        &self,
        py: Python<'py>,
        max_bytes: usize,
        timeout_ms: u64,
    ) -> PyResult<Bound<'py, pyo3::types::PyBytes>> {
        let bytes = py.allow_threads(|| {
            self.inner
                .lock()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("session poisoned"))
                .map(|s| s.drain_stderr(max_bytes, timeout_ms))
        })?;
        Ok(pyo3::types::PyBytes::new(py, &bytes))
    }

    /// Wait for exit up to *timeout_ms*: exit code, or ``None`` on timeout.
    /// Blocks with the GIL released.
    fn wait(&self, py: Python<'_>, timeout_ms: u64) -> PyResult<Option<i32>> {
        py.allow_threads(|| {
            self.inner
                .lock()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("session poisoned"))
                .map(|mut s| s.wait(timeout_ms))
        })
    }

    /// Whether the child has exited.
    fn is_running(&self) -> PyResult<bool> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("session poisoned"))?
            .is_running())
    }

    /// Kill the whole process tree.  On Windows the retained Job Object
    /// handle reaps surviving descendants when the session is dropped.
    fn kill(&self, py: Python<'_>) -> PyResult<()> {
        py.allow_threads(|| {
            self.inner
                .lock()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("session poisoned"))
                .map(|mut s| s.kill())
        })
    }
}

// ---------------------------------------------------------------------------
// Module registration
// ---------------------------------------------------------------------------

#[pymodule]
fn _native(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Existing functions
    m.add_function(wrap_pyfunction!(search_codebase, m)?)?;
    m.add_function(wrap_pyfunction!(read_file, m)?)?;
    m.add_function(wrap_pyfunction!(write_file, m)?)?;
    m.add_function(wrap_pyfunction!(grep, m)?)?;
    m.add_function(wrap_pyfunction!(glob, m)?)?;
    m.add_function(wrap_pyfunction!(count_tokens, m)?)?;
    m.add_function(wrap_pyfunction!(compute_diff, m)?)?;
    m.add_function(wrap_pyfunction!(apply_diff, m)?)?;
    m.add_function(wrap_pyfunction!(sandbox_execute, m)?)?;
    m.add_function(wrap_pyfunction!(sandbox_read_file, m)?)?;
    m.add_function(wrap_pyfunction!(sandbox_write_file, m)?)?;
    m.add_function(wrap_pyfunction!(execute_shell, m)?)?;
    m.add_class::<PySandboxSession>()?;

    // New: embedding
    m.add_function(wrap_pyfunction!(cosine_similarity, m)?)?;
    m.add_function(wrap_pyfunction!(text_similarity, m)?)?;

    // New: simd_search
    m.add_function(wrap_pyfunction!(simd_contains, m)?)?;
    m.add_function(wrap_pyfunction!(simd_find_all, m)?)?;
    m.add_function(wrap_pyfunction!(simd_memmem, m)?)?;

    // New: landlock
    m.add_function(wrap_pyfunction!(landlock_restrict_read_only, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_restrict_network, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_full_sandbox, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_workspace_sandbox, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_plugin_sandbox, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_available, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_abi_version, m)?)?;

    // New: lsp_proto
    m.add_function(wrap_pyfunction!(parse_lsp_message, m)?)?;
    m.add_function(wrap_pyfunction!(parse_diagnostics, m)?)?;
    m.add_function(wrap_pyfunction!(build_lsp_request, m)?)?;
    m.add_function(wrap_pyfunction!(build_content_length_header, m)?)?;

    // New: permission system
    m.add_function(wrap_pyfunction!(permission_check, m)?)?;
    m.add_function(wrap_pyfunction!(permission_check_mode, m)?)?;
    m.add_function(wrap_pyfunction!(permission_set_policies, m)?)?;
    m.add_function(wrap_pyfunction!(permission_get_policies, m)?)?;
    m.add_function(wrap_pyfunction!(permission_set_learned_policies, m)?)?;
    m.add_function(wrap_pyfunction!(permission_get_learned_policies, m)?)?;
    m.add_function(wrap_pyfunction!(permission_record_decision, m)?)?;
    m.add_function(wrap_pyfunction!(permission_set_mode, m)?)?;
    m.add_function(wrap_pyfunction!(permission_set_dangerous_patterns, m)?)?;
    m.add_function(wrap_pyfunction!(permission_set_tool_danger_levels, m)?)?;
    m.add_function(wrap_pyfunction!(permission_check_dangerous, m)?)?;
    m.add_function(wrap_pyfunction!(permission_dangerous_verdict, m)?)?;
    m.add_function(wrap_pyfunction!(permission_dangerous_matches, m)?)?;
    m.add_function(wrap_pyfunction!(permission_check_immune, m)?)?;
    m.add_function(wrap_pyfunction!(permission_check_immune_path, m)?)?;
    m.add_function(wrap_pyfunction!(permission_capability_policy, m)?)?;
    m.add_function(wrap_pyfunction!(permission_add_plugin_policy, m)?)?;
    m.add_function(wrap_pyfunction!(permission_remove_plugin_policies, m)?)?;
    m.add_function(wrap_pyfunction!(permission_plugin_vote, m)?)?;
    m.add_function(wrap_pyfunction!(permission_get_plugin_policies, m)?)?;
    m.add_function(wrap_pyfunction!(permission_set_plugin_resources, m)?)?;
    m.add_function(wrap_pyfunction!(permission_remove_plugin_resources, m)?)?;
    m.add_function(wrap_pyfunction!(permission_check_plugin_resource, m)?)?;
    m.add_function(wrap_pyfunction!(permission_get_plugin_resources, m)?)?;
    m.add_function(wrap_pyfunction!(permission_register_plugin_tool, m)?)?;
    m.add_function(wrap_pyfunction!(permission_set_plugin_tools_active, m)?)?;
    m.add_function(wrap_pyfunction!(permission_plugin_tool_owner, m)?)?;
    m.add_function(wrap_pyfunction!(permission_set_plugin_ui, m)?)?;
    m.add_function(wrap_pyfunction!(permission_remove_plugin_ui, m)?)?;
    m.add_function(wrap_pyfunction!(permission_check_plugin_ui, m)?)?;
    m.add_function(wrap_pyfunction!(plugin_sandbox_prepare_env, m)?)?;
    m.add_function(wrap_pyfunction!(plugin_sandbox_resolve_limits, m)?)?;
    m.add_function(wrap_pyfunction!(ea_build_secret, m)?)?;


    Ok(())
}
