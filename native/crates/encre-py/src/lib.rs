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
use encre::ast;
use encre::codebase;
use encre::diff;
use encre::embedding;
use encre::fs;
use encre::indexer;
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

#[pyfunction]
fn search_codebase(py: Python, query: &str, path: Option<&str>) -> PyResult<Vec<PyObject>> {
    let p = path.unwrap_or(".");
    let results = search::search_codebase(query, p);
    search_results_to_py(py, &results)
}

#[pyfunction]
fn build_code_index(workspace: &str) -> PyResult<String> {
    let data = codebase::build_code_index(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn count_code_index_candidates(workspace: &str) -> PyResult<usize> {
    codebase::count_code_index_candidates(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

#[pyfunction]
fn load_code_index(workspace: &str) -> PyResult<String> {
    let data = codebase::load_code_index(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn update_code_index(workspace: &str) -> PyResult<String> {
    let data = codebase::update_code_index(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn search_code_index(workspace: &str, query: &str, limit: Option<usize>) -> PyResult<String> {
    let results = codebase::search_code_index(workspace, query, limit.unwrap_or(10))
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&results)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn build_code_context(workspace: &str, file_path: &str) -> PyResult<String> {
    codebase::build_code_context(workspace, file_path)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

#[pyfunction]
fn ast_available() -> bool {
    ast::ast_available()
}

#[pyfunction]
fn ast_backend_name() -> String {
    ast::ast_backend_name().to_string()
}

#[pyfunction]
fn build_ast_index(workspace: &str) -> PyResult<String> {
    let data = ast::build_ast_index(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn load_ast_index(workspace: &str) -> PyResult<String> {
    let data = ast::load_ast_index(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn update_ast_index(workspace: &str) -> PyResult<String> {
    let data = ast::update_ast_index(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn ast_get_symbol(workspace: &str, name: &str) -> PyResult<String> {
    let data = ast::ast_get_symbol(workspace, name)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn ast_get_outline(workspace: &str, file: &str) -> PyResult<String> {
    let data = ast::ast_get_outline(workspace, file)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn ast_list_files(workspace: &str) -> PyResult<String> {
    let data = ast::ast_list_files(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
#[pyo3(signature = (workspace, name, limit=10))]
fn ast_find_relevant(workspace: &str, name: &str, limit: usize) -> PyResult<String> {
    let data = ast::ast_find_relevant(workspace, name, limit)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn ast_find_references(workspace: &str, name: &str) -> PyResult<String> {
    let data = ast::ast_find_references(workspace, name)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn ast_goto_definition(workspace: &str, file: &str, line: usize, col: usize) -> PyResult<String> {
    let data = ast::ast_goto_definition(workspace, file, line, col)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
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
// New: embedding.rs bindings
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

#[pyfunction]
#[pyo3(signature = (workspace, max_chars=4000))]
fn build_embedding_slices(workspace: &str, max_chars: usize) -> PyResult<String> {
    let data = embedding::build_embedding_slices(workspace, max_chars)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn load_embedding_index(workspace: &str) -> PyResult<String> {
    let data = embedding::load_embedding_index(workspace)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn save_embedding_index(
    workspace: &str,
    slices_json: &str,
    vectors_json: &str,
    file_mtimes_json: &str,
    embedding_dim: usize,
) -> PyResult<()> {
    let slices: Vec<embedding::EmbeddingSlice> = serde_json::from_str(slices_json)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
    let vectors: Vec<Vec<f32>> = serde_json::from_str(vectors_json)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
    let file_mtimes: std::collections::HashMap<String, f64> = serde_json::from_str(file_mtimes_json)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
    embedding::save_embedding_index(workspace, slices, vectors, file_mtimes, embedding_dim)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))
}

#[pyfunction]
fn search_embedding_index(workspace: &str, query_vector_json: &str, k: usize) -> PyResult<String> {
    let query_vector: Vec<f32> = serde_json::from_str(query_vector_json)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
    let data = embedding::search_embedding_index(workspace, query_vector, k)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    serde_json::to_string(&data)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
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
// BM25 Indexer (PyO3 class)
// ---------------------------------------------------------------------------

/// BM25-powered code search index.
///
/// Usage from Python::
///
///     from encre._native import Bm25Index
///     idx = Bm25Index()
///     idx.build([("path.py", "def foo(): pass")])
///     results = idx.search("foo", 10)
#[pyclass]
struct Bm25Index {
    inner: indexer::Bm25Index,
}

#[pymethods]
impl Bm25Index {
    #[new]
    fn new() -> Self {
        Bm25Index {
            inner: indexer::Bm25Index::new(),
        }
    }

    /// Build or rebuild the index from a list of (path, content) pairs.
    fn build(&mut self, files: Vec<(String, String)>) {
        self.inner.build(files);
    }

    /// Search with BM25 ranking. Returns list of (path, score).
    fn search(&self, query: &str, limit: Option<usize>) -> Vec<(String, f64)> {
        self.inner
            .search(query, limit.unwrap_or(10), 1.5, 0.75, 2.0)
    }

    /// Add or update a single document.
    fn add_document(&mut self, path: &str, content: &str) {
        self.inner.add_document(path, content);
    }

    /// Remove a document by path.
    fn remove_document(&mut self, path: &str) {
        self.inner.remove_document(path);
    }

    /// Clear the entire index.
    fn clear(&mut self) {
        self.inner.clear();
    }

    /// Number of indexed documents.
    fn __len__(&self) -> usize {
        self.inner.len()
    }

    fn __bool__(&self) -> bool {
        !self.inner.is_empty()
    }
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

// ---------------------------------------------------------------------------
// Module registration
// ---------------------------------------------------------------------------

#[pymodule]
fn _native(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Existing functions
    m.add_function(wrap_pyfunction!(search_codebase, m)?)?;
    m.add_function(wrap_pyfunction!(count_code_index_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(build_code_index, m)?)?;
    m.add_function(wrap_pyfunction!(load_code_index, m)?)?;
    m.add_function(wrap_pyfunction!(update_code_index, m)?)?;
    m.add_function(wrap_pyfunction!(search_code_index, m)?)?;
    m.add_function(wrap_pyfunction!(build_code_context, m)?)?;
    m.add_function(wrap_pyfunction!(ast_available, m)?)?;
    m.add_function(wrap_pyfunction!(ast_backend_name, m)?)?;
    m.add_function(wrap_pyfunction!(build_ast_index, m)?)?;
    m.add_function(wrap_pyfunction!(load_ast_index, m)?)?;
    m.add_function(wrap_pyfunction!(update_ast_index, m)?)?;
    m.add_function(wrap_pyfunction!(ast_get_symbol, m)?)?;
    m.add_function(wrap_pyfunction!(ast_get_outline, m)?)?;
    m.add_function(wrap_pyfunction!(ast_list_files, m)?)?;
    m.add_function(wrap_pyfunction!(ast_find_relevant, m)?)?;
    m.add_function(wrap_pyfunction!(ast_find_references, m)?)?;
    m.add_function(wrap_pyfunction!(ast_goto_definition, m)?)?;
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

    // New: embedding
    m.add_function(wrap_pyfunction!(cosine_similarity, m)?)?;
    m.add_function(wrap_pyfunction!(text_similarity, m)?)?;
    m.add_function(wrap_pyfunction!(build_embedding_slices, m)?)?;
    m.add_function(wrap_pyfunction!(load_embedding_index, m)?)?;
    m.add_function(wrap_pyfunction!(save_embedding_index, m)?)?;
    m.add_function(wrap_pyfunction!(search_embedding_index, m)?)?;

    // New: simd_search
    m.add_function(wrap_pyfunction!(simd_contains, m)?)?;
    m.add_function(wrap_pyfunction!(simd_find_all, m)?)?;
    m.add_function(wrap_pyfunction!(simd_memmem, m)?)?;

    // New: landlock
    m.add_function(wrap_pyfunction!(landlock_restrict_read_only, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_restrict_network, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_full_sandbox, m)?)?;
    m.add_function(wrap_pyfunction!(landlock_workspace_sandbox, m)?)?;
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
    m.add_function(wrap_pyfunction!(permission_check_dangerous, m)?)?;

    // BM25 indexer
    m.add_class::<Bm25Index>()?;

    Ok(())
}
