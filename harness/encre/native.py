#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

"""Native acceleration bridge -- all model-facing operations are Rust-only.

All functions are re-exported directly from the compiled ``encre._native``
Rust extension.  If the extension is not installed, Python's normal
``ModuleNotFoundError`` applies -- build it with::

    cd native && cargo build --release
    # copy target/release/_native.{dll,so} -> backend/encre/_native.pyd
"""

try:
    import encre._native as _native
except ImportError:
    # The compiled extension is optional at import time: downstream modules
    # (code search, tokenizer fallbacks, ...) must stay importable on
    # machines without the built binary.  Every re-export below resolves to
    # a clear RuntimeError stub instead of hard-failing the whole package.
    _native = None

_NATIVE_MISSING_MSG = (
    "Native function '{name}' is unavailable: the encre._native binary is not "
    "installed. Build it with `cd native && cargo build --release` and copy "
    "target/release/_native.{{dll,so,pyd}} next to the encre package."
)

#: True when the compiled Rust extension loaded successfully.
_HAS_NATIVE = _native is not None


def _missing_native(name: str):
    def _raiser(*_args, **_kwargs):
        raise RuntimeError(_NATIVE_MISSING_MSG.format(name=name))

    return _raiser


def _reexport(name: str):
    """Return the native symbol, or a raising stub when native is absent."""
    if _native is None:
        return _missing_native(name)
    return getattr(_native, name, _missing_native(name))


def _reexport_default(name: str, default):
    if _native is None:
        return default
    return getattr(_native, name, default)


Bm25Index = _reexport("Bm25Index")
apply_diff = _reexport("apply_diff")
build_code_context = _reexport("build_code_context")
build_code_index = _reexport("build_code_index")
count_code_index_candidates = _reexport("count_code_index_candidates")
build_content_length_header = _reexport("build_content_length_header")
build_lsp_request = _reexport("build_lsp_request")
compute_diff = _reexport("compute_diff")
cosine_similarity = _reexport("cosine_similarity")
build_embedding_slices = _reexport("build_embedding_slices")
count_tokens = _reexport("count_tokens")
execute_shell = _reexport("execute_shell")
glob = _reexport("glob")
grep = _reexport("grep")
landlock_abi_version = _reexport("landlock_abi_version")
landlock_available = _reexport("landlock_available")
landlock_full_sandbox = _reexport("landlock_full_sandbox")
landlock_restrict_network = _reexport("landlock_restrict_network")
landlock_restrict_read_only = _reexport("landlock_restrict_read_only")
landlock_workspace_sandbox = _reexport("landlock_workspace_sandbox")
load_code_index = _reexport("load_code_index")
load_embedding_index = _reexport("load_embedding_index")
update_code_index = _reexport("update_code_index")
parse_diagnostics = _reexport("parse_diagnostics")
parse_lsp_message = _reexport("parse_lsp_message")
permission_check = _reexport("permission_check")
permission_check_mode = _reexport("permission_check_mode")
permission_check_dangerous = _reexport("permission_check_dangerous")
permission_get_policies = _reexport("permission_get_policies")
permission_get_learned_policies = _reexport("permission_get_learned_policies")
permission_record_decision = _reexport("permission_record_decision")
permission_set_dangerous_patterns = _reexport("permission_set_dangerous_patterns")
permission_set_learned_policies = _reexport("permission_set_learned_policies")
permission_set_mode = _reexport("permission_set_mode")
permission_set_policies = _reexport("permission_set_policies")
read_file = _reexport("read_file")
sandbox_execute = _reexport("sandbox_execute")
sandbox_read_file = _reexport("sandbox_read_file")
sandbox_write_file = _reexport("sandbox_write_file")
save_embedding_index = _reexport("save_embedding_index")
search_embedding_index = _reexport("search_embedding_index")
search_code_index = _reexport("search_code_index")
search_codebase = _reexport("search_codebase")


def _with_default_args(fn, *defaults_by_pos):
    """Wrap *fn* so missing positional args fall back to per-position defaults.

    ``defaults_by_pos[i]`` is the default for argument index ``i``; ``None``
    entries mean "no default for this position".
    """
    def _wrapped(*args, **kwargs):
        padded = list(args)
        for i, d in enumerate(defaults_by_pos):
            if len(padded) <= i and d is not None:
                padded.append(d)
        return fn(*padded, **kwargs)
    return _wrapped


if _native is not None:
    # The Rust symbol requires an explicit path; tests and callers expect a
    # default of the current directory when none is supplied.  (_missing_native
    # stubs only exist when _native is None, so no double-wrap is possible.)
    search_codebase = _with_default_args(search_codebase, None, ".")
simd_contains = _reexport("simd_contains")
simd_find_all = _reexport("simd_find_all")
simd_memmem = _reexport("simd_memmem")
text_similarity = _reexport("text_similarity")
write_file = _reexport("write_file")

ast_available = _reexport_default("ast_available", lambda: False)
ast_backend_name = _reexport_default("ast_backend_name", lambda: None)
ast_find_references = _reexport("ast_find_references")
ast_find_relevant = _reexport("ast_find_relevant")
ast_get_outline = _reexport("ast_get_outline")
ast_get_symbol = _reexport("ast_get_symbol")
ast_goto_definition = _reexport("ast_goto_definition")
ast_list_files = _reexport("ast_list_files")
build_ast_index = _reexport("build_ast_index")
load_ast_index = _reexport("load_ast_index")
update_ast_index = _reexport("update_ast_index")

# Backward-compatible alias
glob_pattern = glob


__all__ = [
    "Bm25Index",
    "apply_diff",
    "ast_available",
    "ast_backend_name",
    "ast_find_references",
    "ast_find_relevant",
    "ast_get_outline",
    "ast_get_symbol",
    "ast_goto_definition",
    "ast_list_files",
    "build_ast_index",
    "build_code_context",
    "build_code_index",
    "build_content_length_header",
    "build_embedding_slices",
    "build_lsp_request",
    "compute_diff",
    "cosine_similarity",
    "count_code_index_candidates",
    "count_tokens",
    "execute_shell",
    "glob",
    "glob_pattern",
    "grep",
    "landlock_abi_version",
    "landlock_available",
    "landlock_full_sandbox",
    "landlock_restrict_network",
    "landlock_restrict_read_only",
    "landlock_workspace_sandbox",
    "load_ast_index",
    "load_code_index",
    "load_embedding_index",
    "parse_diagnostics",
    "parse_lsp_message",
    "permission_check",
    "permission_check_dangerous",
    "permission_check_mode",
    "permission_get_learned_policies",
    "permission_get_policies",
    "permission_record_decision",
    "permission_set_dangerous_patterns",
    "permission_set_learned_policies",
    "permission_set_mode",
    "permission_set_policies",
    "read_file",
    "sandbox_execute",
    "sandbox_read_file",
    "sandbox_write_file",
    "save_embedding_index",
    "search_code_index",
    "search_codebase",
    "search_embedding_index",
    "simd_contains",
    "simd_find_all",
    "simd_memmem",
    "text_similarity",
    "update_ast_index",
    "update_code_index",
    "write_file",
]
