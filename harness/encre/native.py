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

import encre._native as _native


def _missing_native(name: str):
    def _raiser(*_args, **_kwargs):
        raise RuntimeError(
            f"Native function '{name}' is unavailable in the installed encre._native binary. "
            "Rebuild backend/encre/_native.pyd from native/crates/encre-py."
        )

    return _raiser


Bm25Index = _native.Bm25Index
apply_diff = _native.apply_diff
build_code_context = _native.build_code_context
build_code_index = _native.build_code_index
count_code_index_candidates = _native.count_code_index_candidates
build_content_length_header = _native.build_content_length_header
build_lsp_request = _native.build_lsp_request
compute_diff = _native.compute_diff
cosine_similarity = _native.cosine_similarity
build_embedding_slices = getattr(_native, "build_embedding_slices", _missing_native("build_embedding_slices"))
count_tokens = _native.count_tokens
execute_shell = _native.execute_shell
glob = _native.glob
grep = _native.grep
landlock_abi_version = _native.landlock_abi_version
landlock_available = _native.landlock_available
landlock_full_sandbox = _native.landlock_full_sandbox
landlock_restrict_network = _native.landlock_restrict_network
landlock_restrict_read_only = _native.landlock_restrict_read_only
landlock_workspace_sandbox = _native.landlock_workspace_sandbox
load_code_index = _native.load_code_index
load_embedding_index = getattr(_native, "load_embedding_index", _missing_native("load_embedding_index"))
update_code_index = _native.update_code_index
parse_diagnostics = _native.parse_diagnostics
parse_lsp_message = _native.parse_lsp_message
permission_check = _native.permission_check
permission_check_mode = getattr(_native, "permission_check_mode", _missing_native("permission_check_mode"))
permission_check_dangerous = getattr(_native, "permission_check_dangerous", _missing_native("permission_check_dangerous"))
permission_get_policies = _native.permission_get_policies
permission_record_decision = _native.permission_record_decision
permission_set_dangerous_patterns = getattr(
    _native, "permission_set_dangerous_patterns",
    _missing_native("permission_set_dangerous_patterns"),
)
permission_set_mode = getattr(_native, "permission_set_mode", _missing_native("permission_set_mode"))
permission_set_policies = _native.permission_set_policies
read_file = _native.read_file
sandbox_execute = _native.sandbox_execute
sandbox_read_file = _native.sandbox_read_file
sandbox_write_file = _native.sandbox_write_file
save_embedding_index = getattr(_native, "save_embedding_index", _missing_native("save_embedding_index"))
search_embedding_index = getattr(_native, "search_embedding_index", _missing_native("search_embedding_index"))
search_code_index = _native.search_code_index
search_codebase = _native.search_codebase
simd_contains = _native.simd_contains
simd_find_all = _native.simd_find_all
simd_memmem = _native.simd_memmem
text_similarity = _native.text_similarity
write_file = _native.write_file

ast_available = getattr(_native, "ast_available", lambda: False)
ast_backend_name = getattr(_native, "ast_backend_name", lambda: None)
ast_find_references = getattr(_native, "ast_find_references", _missing_native("ast_find_references"))
ast_find_relevant = getattr(_native, "ast_find_relevant", _missing_native("ast_find_relevant"))
ast_get_outline = getattr(_native, "ast_get_outline", _missing_native("ast_get_outline"))
ast_get_symbol = getattr(_native, "ast_get_symbol", _missing_native("ast_get_symbol"))
ast_goto_definition = getattr(_native, "ast_goto_definition", _missing_native("ast_goto_definition"))
ast_list_files = getattr(_native, "ast_list_files", _missing_native("ast_list_files"))
build_ast_index = getattr(_native, "build_ast_index", _missing_native("build_ast_index"))
load_ast_index = getattr(_native, "load_ast_index", _missing_native("load_ast_index"))
update_ast_index = getattr(_native, "update_ast_index", _missing_native("update_ast_index"))

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
    "permission_get_policies",
    "permission_record_decision",
    "permission_set_dangerous_patterns",
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
