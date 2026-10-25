#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
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

"""Static analyzer for the ``ai-code-doctor`` skill.

This script is the analysis engine that the ai-code-doctor skill invokes
on a target Python file or directory. It is a pure read-only tool — it
never writes to disk, never imports the analyzed code, and never
executes it. All findings are derived from the AST.

Input:
    A single Python file path or a directory path, passed as
    ``sys.argv[1]``. Directories are walked recursively, excluding
    VCS directories, virtualenvs, ``__pycache__``, and
    ``node_modules``.

Output:
    JSON on stdout. For each analyzed file the report contains:

    * **Per-function metrics**: McCabe cyclomatic complexity, argument
      count, and a structural signature used for duplicate detection.
    * **Duplicate code blocks**: groups of functions whose structural
      signatures match (names and constants stripped, so two functions
      that differ only in identifier names are still detected as
      duplicates).
    * **Hub functions**: functions called from two or more distinct
      caller functions. High hub count suggests tight coupling.
    * **I/O and DB call points**: locations where the code touches the
      file system, network, or database, so the skill can flag them in
      its review.
    * **File-level summary**: counts and maxima aggregated across the
      file's functions.

    An aggregate summary across all files is included at the top level.

Robustness contract:
    This script must never crash. Syntax errors, empty files, binary
    files, missing paths, and non-Python input are all handled
    gracefully — the script always emits valid JSON and exits with a
    non-zero code only on usage errors. This is because the script is
    invoked by an LLM-driven skill, and a crash would propagate as a
    tool error rather than being recoverable analysis output.

Public entry points:

* :func:`analyze` — top-level function; returns a result dict for a
  file or directory.
* :func:`analyze_file` — analyze a single file; returns a result dict.
* :func:`analyze_tree` — analyze a parsed AST; used internally and in
  unit tests.
* :func:`main` — CLI entry point; called from the ``__main__`` guard.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from collections import defaultdict


# AST node types that each add one branch to a function's cyclomatic
# complexity. This is the canonical McCabe set: conditionals, loops,
# exception handlers, and assertions. Each occurrence adds exactly 1
# to the complexity score.
DECISION_TYPES = (
    ast.If,
    ast.IfExp,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.Assert,
)

# Comprehension and generator expression node types. These are counted
# specially: each comprehension contributes 1 per generator clause plus
# 1 per filter clause (the ``if`` clauses inside the comprehension),
# which mirrors how a human would read a nested comprehension as a
# series of conditional filters.
COMP_TYPES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

# Bare function names that indicate I/O when called directly. ``open``
# and ``print`` cover the file-system and stdout/stderr paths;
# ``urlopen`` is a legacy HTTP fetch that some code still uses.
IO_CALL_NAMES = {
    "open",
    "print",
    "urlopen",
}

# Module names that indicate I/O when their functions are called. Any
# call of the form ``requests.get(...)`` or ``urllib.request.urlopen``
# is flagged because these modules own HTTP sockets. ``socket`` and
# ``http`` cover the low-level network primitives; ``aiohttp`` is the
# async counterpart used in the harness's own HTTP clients.
IO_MODULES = {
    "requests",
    "urllib",
    "socket",
    "http",
    "aiohttp",
}

# Attribute call names that indicate I/O when called on any object.
# These are method names that commonly read from or write to streams,
# sockets, or other I/O objects. We match on the attribute name
# regardless of the receiver type because the AST does not carry type
# information — this is a heuristic, not a precise type check.
IO_CALL_ATTRS = {
    "read",
    "readline",
    "readlines",
    "write",
    "writelines",
    "send",
    "recv",
}

# Direct database call attributes. These are method names on DB API
# cursor objects (``execute``, ``fetchall``, etc.) and Django ORM
# queryset methods (``bulk_create``, ``aggregate``, ``annotate``).
# The analyzer flags any call whose attribute name matches, regardless
# of the receiver.
DB_DIRECT_ATTRS = {
    "execute",
    "executemany",
    "executescript",
    "fetchone",
    "fetchall",
    "fetchmany",
    "commit",
    "rollback",
    "bulk_create",
    "aggregate",
    "annotate",
}

# ORM queryset call attributes (Django-style). These are chained on a
# queryset, so the analyzer looks for the pattern
# ``Model.objects.<one_of_these>(...)`` by checking that the call's
# value is an attribute access whose attribute name is in
# :data:`DB_ATTR_ACCESS` (typically ``objects`` or ``query``).
ORM_CALL_ATTRS = {
    "filter",
    "all",
    "exclude",
    "get",
    "select_related",
    "prefetch_related",
    "update",
    "delete",
}

# Attribute names that indicate the receiver is an ORM manager. In
# Django models, ``Model.objects`` and ``Model.query`` are the
# conventional entry points to the query interface. The analyzer uses
# these to identify ORM usage in the
# ``Model.objects.filter(...)`` pattern.
DB_ATTR_ACCESS = {
    "objects",
    "query",
}

# Directory names to skip during recursive directory walks. This is a
# lowercased set so the comparison is case-insensitive on Windows and
# Linux alike. The set covers VCS directories (``.git``, ``.hg``,
# ``.svn``), Python virtualenvs (``.venv``, ``venv``, ``env``),
# Python's own cache (``__pycache__``), Node.js dependencies
# (``node_modules``), and globally-installed Python packages
# (``site-packages``).
EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "site-packages",
}


def _decision_contribution(node):
    """Return the cyclomatic-complexity contribution of a single AST node.

    This is the per-node contribution to McCabe complexity:

    * Boolean operators (``and``, ``or``) contribute ``len(values) - 1``
      because each additional operand is effectively an extra branch.
    * Comprehensions contribute ``1 + len(ifs)`` per generator clause,
      mirroring how each ``for x in y`` is a loop and each ``if z`` is
      a filter.
    * Decision nodes (``if``, ``for``, ``while``, ``except``,
      ``assert``) each contribute exactly 1.
    * All other nodes contribute 0.

    The function never raises; it returns 0 for unknown node types so
    that the caller's summation is unaffected by new Python syntax.
    """
    if isinstance(node, ast.BoolOp):
        return max(0, len(node.values) - 1)
    if isinstance(node, COMP_TYPES):
        total = 0
        for gen in node.generators:
            total += 1
            total += len(gen.ifs)
        return total
    if isinstance(node, DECISION_TYPES):
        return 1
    return 0


def _count_complexity(node):
    """Recursively sum the cyclomatic-complexity contributions of a subtree.

    Walks the subtree rooted at *node* and accumulates
    :func:`_decision_contribution` values. Nested function and lambda
    definitions are skipped because their complexity is counted
    separately (they are separate functions in the report), so counting
    them here would double-count.
    """
    total = _decision_contribution(node)
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        total += _count_complexity(child)
    return total


def function_complexity(func_node):
    """McCabe complexity of a function: 1 + sum of body decision contributions.

    The base score of 1 represents the function's entry point; each
    decision adds a branch. The body statements are walked; nested
    functions and lambdas inside the body are skipped because their
    complexity is reported separately for those nested definitions.

    Args:
        func_node: An ``ast.FunctionDef`` or ``ast.AsyncFunctionDef``
            node whose complexity is to be computed.

    Returns:
        An integer complexity score. A function with no decisions
        scores 1; each conditional, loop, or comprehension clause adds
        to that base.
    """
    complexity = 1
    for stmt in func_node.body:
        complexity += _count_complexity(stmt)
    return complexity


def _func_display_name(node):
    """Build a dotted display name for a function or class node.

    Walks up the parent chain (populated by :func:`_annotate_parents`)
    to produce names like ``Module.Class.method`` rather than just
    ``method``. This makes the report readable when multiple functions
    share the same simple name — for example, ``Foo.validate`` and
    ``Bar.validate`` are distinct.

    The parent chain is walked only as long as the parents are function
    or class definitions; a module-level function returns just its own
    name, a method returns ``Class.method``, and a nested function
    returns ``Outer.inner``.

    Args:
        node: The function or class node to name.

    Returns:
        A dotted name string, e.g. ``"MyClass.my_method"`` or
        ``"my_function"`` for top-level functions.
    """
    parts = []
    cur = node
    while isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        parts.append(cur.name)
        cur = cur.parent if hasattr(cur, "parent") else None
        if cur is None:
            break
    parts.reverse()
    return ".".join(parts) if parts else node.name


def _annotate_parents(tree):
    """Attach a ``parent`` attribute to every child node in the AST.

    The standard :mod:`ast` module does not track parent relationships,
    but several downstream functions (like :func:`_enclosing_node` and
    :func:`_func_display_name`) need to walk up from a call site to its
    enclosing function or class. This function populates a ``parent``
    attribute on every child node in place.

    The traversal is a simple breadth-first walk via
    :func:`ast.iter_child_nodes`. The function is idempotent: calling
    it twice produces the same result.

    Args:
        tree: The root of the AST (typically an ``ast.Module``).
    """
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent


def _struct_sig(node):
    """Structural signature with names/constants stripped, for dup detection.

    Produces a string that captures the AST structure of a subtree but
    ignores all identifier names and constant values. Two functions
    whose bodies differ only in variable names, string literals, or
    numeric constants will produce the same signature, which is the
    basis for the duplicate-code detection in :func:`analyze_tree`.

    The signature is built recursively:

    * ``None`` becomes ``"N"``.
    * ``ast.Name`` nodes become the literal string ``"Name"`` (the
      identifier is discarded).
    * ``ast.Constant`` nodes become ``"Const"`` (the value is
      discarded).
    * ``ast.Attribute`` becomes ``"Attr(<child_sig>)"`` — the attribute
      name is discarded but the receiver's structure is preserved.
    * All other nodes become ``"<TypeName>(<child_sigs>)"``.

    Args:
        node: An AST node (or ``None``) to sign.

    Returns:
        A string signature. Two nodes with the same shape but
        different names/constants produce the same signature.
    """
    if node is None:
        return "N"
    if isinstance(node, ast.Name):
        return "Name"
    if isinstance(node, ast.Constant):
        return "Const"
    if isinstance(node, ast.Attribute):
        return "Attr(" + _struct_sig(node.value) + ")"
    cls = type(node).__name__
    children = [_struct_sig(c) for c in ast.iter_child_nodes(node)]
    if children:
        return cls + "(" + ",".join(children) + ")"
    return cls


def _called_name(call_node):
    """Return a readable name for what a Call invokes, or None.

    Extracts a human-readable identifier for the function being called.
    For a bare call like ``foo(...)`` the result is ``"foo"``; for an
    attribute call like ``bar.baz(...)`` the result is ``"baz"``. Calls
    that do not fit either pattern (for example, a call on a
    sub-expression like ``(x).foo()`` or ``[1,2][0]()``) return
    ``None``.

    Args:
        call_node: An ``ast.Call`` node.

    Returns:
        A string name if the call target is a simple ``Name`` or
        ``Attribute`` node; otherwise ``None``.
    """
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _enclosing_node(node, node_types):
    """Walk up the parent chain to find the nearest ancestor of a given type.

    Uses the ``parent`` attribute populated by :func:`_annotate_parents`
    to walk up from *node* until an ancestor matching *node_types* is
    found, or the module root is reached.

    Args:
        node: The starting node.
        node_types: A tuple of AST node types to match. Typically
            ``(ast.FunctionDef, ast.AsyncFunctionDef)`` to find the
            enclosing function, or ``(ast.ClassDef,)`` to find the
            enclosing class.

    Returns:
        The nearest matching ancestor, or ``None`` if none is found
        (for example, a top-level call with no enclosing function).
    """
    parent = getattr(node, "parent", None)
    while parent is not None:
        if isinstance(parent, node_types):
            return parent
        parent = getattr(parent, "parent", None)
    return None


def _resolved_call_target(call_node, defined_top_level, defined_qualified):
    """Resolve a Call node to the qualified name of the called function.

    The function is conservative: it only resolves calls whose target
    is either a top-level function defined in this module, or a method
    call on ``self`` / ``cls`` that can be resolved to a method defined
    in the enclosing class. Calls that target names not defined in this
    module (for example, imports from other modules) return ``None``
    because the analyzer has no cross-module symbol table.

    Args:
        call_node: The ``ast.Call`` node to resolve.
        defined_top_level: A set of function names defined at module
            level in this file.
        defined_qualified: A set of fully-qualified function names
            (e.g. ``"MyClass.my_method"``) defined in this file.

    Returns:
        The qualified name of the called function, or ``None`` if the
        call cannot be resolved within this module's scope.
    """
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id if func.id in defined_top_level else None
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    if func.value.id in {"self", "cls"}:
        class_node = _enclosing_node(call_node, ast.ClassDef)
        if class_node is None:
            return None
        candidate = "%s.%s" % (_func_display_name(class_node), func.attr)
    else:
        candidate = "%s.%s" % (func.value.id, func.attr)
    return candidate if candidate in defined_qualified else None


def _source_segment(source, node):
    """Best-effort source snippet for a node, using its line range.

    Extracts the source lines corresponding to a node's ``lineno`` and
    ``end_lineno`` attributes. This is a best-effort extraction: if the
    node lacks line information (for example, in a synthetic AST), the
    function returns an empty string rather than raising.

    Args:
        source: The full source text of the file.
        node: An AST node with ``lineno`` and optionally ``end_lineno``
            attributes.

    Returns:
        The source text spanning the node's lines, joined with
        newlines, or an empty string if the node has no valid line
        range.
    """
    try:
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        lines = source.splitlines()
        seg = "\n".join(lines[start - 1:end])
        return seg
    except Exception:
        return ""


def analyze_tree(tree, source, path):
    """Analyze a parsed AST. Returns a dict of findings for one file.

    This is the core analysis function. It walks the AST once to collect
    function definitions and their qualified names, then walks again to
    gather per-function metrics, duplicate groups, hub functions, and
    I/O/DB call points.

    The analysis is intentionally conservative: it reports only what it
    can confirm from the AST alone. It does not perform type inference,
    does not follow imports, and does not evaluate expressions. The
    result is a dict suitable for JSON serialization.

    Args:
        tree: The parsed AST (root ``ast.Module``).
        source: The original source text, used for line-number-based
            snippet extraction (currently unused by the output but kept
            for API compatibility and future features).
        path: The file path being analyzed, included in error
            messages for context.

    Returns:
        A dict with keys:

        * ``functions``: list of per-function records with name, line,
          end_line, complexity, arg_count, struct_hash.
        * ``max_complexity``: the highest complexity across all
          functions in the file.
        * ``duplicates``: groups of functions sharing a structural
          signature, sorted by group size descending.
        * ``hubs``: functions called from 2+ distinct callers, sorted
          by caller count descending.
        * ``io_db_points``: locations where I/O or DB calls were
          detected.
    """
    # First pass: annotate parent pointers so downstream functions can
    # walk up from call sites to their enclosing scope.
    _annotate_parents(tree)

    functions = []
    dup_groups = defaultdict(list)
    # calls_by_target maps a called function name to the set of caller
    # function names. A function appearing as a target with 2+ callers
    # is a "hub" — a sign of tight coupling.
    calls_by_target = defaultdict(set)
    io_db_points = []

    # Sets of function names defined in this module, used by
    # _resolved_call_target to determine whether a call can be
    # resolved locally. Top-level functions are keyed by simple name;
    # methods and nested functions are keyed by qualified name.
    defined_top_level = set()
    defined_qualified = set()

    # Pass 1: collect all function definitions and their qualified
    # names. This runs before the main analysis pass so that
    # _resolved_call_target can look up call targets against a
    # complete definition set.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_qualified.add(_func_display_name(node))
            if isinstance(getattr(node, "parent", None), ast.Module):
                defined_top_level.add(node.name)

    # Pass 2: the main analysis walk. Each node is visited exactly once
    # via ast.walk; the per-node type check dispatches to the
    # appropriate handler.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = _func_display_name(node)
            complexity = function_complexity(node)
            sig = _struct_sig(node)
            functions.append({
                "name": name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "complexity": complexity,
                "arg_count": len(node.args.args),
                "struct_hash": sig,
            })
            dup_groups[sig].append({"name": name, "line": node.lineno})

        if isinstance(node, ast.Call):
            called = _called_name(node)
            caller_node = _enclosing_node(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            caller = _func_display_name(caller_node) if caller_node is not None else None

            if called is not None:
                # Try to resolve the call to a function defined in this
                # module. If both the caller and target are known and
                # distinct, record the call edge. The ``caller != target``
                # check excludes self-recursion from hub detection.
                target = _resolved_call_target(node, defined_top_level, defined_qualified)
                if caller is not None and target is not None and caller != target:
                    calls_by_target[target].add(caller)

                # Classify the call as I/O or DB based on the callee
                # name and receiver pattern. The checks are deliberately
                # simple attribute-name matches because the AST does not
                # carry type information.
                is_io = False
                is_db = False
                func = node.func
                if isinstance(func, ast.Name) and func.id in IO_CALL_NAMES:
                    is_io = True
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name) and func.value.id in IO_MODULES:
                        is_io = True
                    elif func.attr in IO_CALL_ATTRS:
                        is_io = True
                    elif func.attr in DB_DIRECT_ATTRS:
                        is_db = True
                    elif (
                        func.attr in ORM_CALL_ATTRS
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr in DB_ATTR_ACCESS
                    ):
                        is_db = True

                if is_io or is_db:
                    io_db_points.append({
                        "line": node.lineno,
                        "caller": caller or "<module>",
                        "kind": "db" if is_db else "io",
                        "call": called,
                    })

        if isinstance(node, ast.Attribute):
            # Detect bare ORM manager attribute access like
            # ``Model.objects.all()`` where the attribute itself is the
            # DB-touching operation. We skip the case where the
            # attribute is part of a Call's func (``.func is parent``)
            # because that was already handled by the ast.Call branch
            # above; counting it again would double-report.
            if node.attr in DB_ATTR_ACCESS:
                parent = getattr(node, "parent", None)
                call_parent = getattr(parent, "parent", None)
                if (
                    isinstance(parent, ast.Attribute)
                    and isinstance(call_parent, ast.Call)
                    and call_parent.func is parent
                ):
                    continue
                caller_node = _enclosing_node(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                caller = _func_display_name(caller_node) if caller_node is not None else None
                io_db_points.append({
                    "line": node.lineno,
                    "caller": caller or "<module>",
                    "kind": "db",
                    "call": "." + node.attr,
                })

    # Group functions by structural signature; any group with 2+ members
    # is a potential duplicate. Sort by group size descending so the
    # largest duplicate clusters appear first in the report.
    duplicates = []
    for sig, group in dup_groups.items():
        if len(group) >= 2:
            duplicates.append({
                "count": len(group),
                "members": group,
            })
    duplicates.sort(key=lambda g: g["count"], reverse=True)

    # Hub functions: targets called from 2+ distinct callers. Sort by
    # caller count descending.
    hubs = []
    for called, callers in calls_by_target.items():
        if len(callers) >= 2:
            hubs.append({
                "name": called,
                "caller_count": len(callers),
                "callers": sorted(callers),
            })
    hubs.sort(key=lambda h: h["caller_count"], reverse=True)

    max_complexity = max((f["complexity"] for f in functions), default=0)

    return {
        "functions": functions,
        "max_complexity": max_complexity,
        "duplicates": duplicates,
        "hubs": hubs,
        "io_db_points": io_db_points,
    }


def analyze_file(path):
    """Analyze a single file. Never raises; returns error info on failure.

    The function is designed to be a total function: it accepts any
    string path and returns a result dict. Failures (missing file,
    non-Python file, syntax error, decode error) are reported in the
    ``error`` field with a ``skipped=True`` marker, so the caller can
    distinguish "analyzed successfully" from "skipped with reason".

    The function reads the file with ``utf-8-sig`` encoding to handle
    both plain UTF-8 and UTF-8-with-BOM files. If the file is not valid
    UTF-8, it falls back to a lossy decode with ``errors="replace"``
    so that partially-readable files still produce analysis rather
    than being dropped entirely.

    Args:
        path: The filesystem path to analyze.

    Returns:
        A dict with at minimum ``file`` and ``skipped`` keys. On
        success the dict also includes ``functions``,
        ``max_complexity``, ``duplicates``, ``hubs``,
        ``io_db_points``, and ``function_count``. On failure the dict
        includes an ``error`` field describing what went wrong.
    """
    result = {"file": path}

    if not path.endswith(".py"):
        result["error"] = "not_python"
        result["skipped"] = True
        return result

    try:
        # utf-8-sig accepts ordinary UTF-8 and Python files carrying a BOM.
        with open(path, "r", encoding="utf-8-sig") as f:
            source = f.read()
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                source = f.read()
        except Exception as e:
            result["error"] = "read_failed: %s" % str(e)
            result["skipped"] = True
            return result
    except Exception as e:
        result["error"] = "read_failed: %s" % str(e)
        result["skipped"] = True
        return result

    if not source.strip():
        result["error"] = "empty_file"
        result["skipped"] = True
        return result

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        result["error"] = "syntax_error: %s (line %s)" % (e.msg, e.lineno or "?")
        result["syntax_error"] = True
        result["skipped"] = True
        return result
    except Exception as e:
        result["error"] = "parse_failed: %s" % str(e)
        result["skipped"] = True
        return result

    try:
        findings = analyze_tree(tree, source, path)
        result.update(findings)
        result["function_count"] = len(findings["functions"])
        result["skipped"] = False
    except Exception as e:
        result["error"] = "analyze_failed: %s" % str(e)
        result["skipped"] = True

    return result


def collect_files(path):
    """Collect Python files from a file or directory path.

    If *path* is a directory, walks it recursively, excluding
    :data:`EXCLUDED_DIR_NAMES` directories. Files are returned in a
    deterministic order (sorted by full path) so that repeated runs
    produce identical output.

    If *path* is a file, it is returned as a single-element list
    regardless of extension — the caller (:func:`analyze_file`) is
    responsible for filtering non-Python files.

    Args:
        path: A filesystem path to a file or directory.

    Returns:
        A sorted list of absolute-ish paths to Python files. Empty
        list if the path does not exist or contains no Python files.
    """
    if os.path.isdir(path):
        files = []
        for root, dirs, names in os.walk(path):
            # Filter the dirs list in place so os.walk does not descend
            # into excluded directories. The comparison is
            # case-insensitive so Windows paths like ".Venv" are also
            # excluded.
            dirs[:] = sorted(name for name in dirs if name.lower() not in EXCLUDED_DIR_NAMES)
            for n in sorted(names):
                if n.endswith(".py"):
                    files.append(os.path.join(root, n))
        return sorted(files)
    if os.path.isfile(path):
        return [path]
    return []


def analyze(path):
    """Top-level analysis entry point.

    Accepts a file or directory path and returns a complete analysis
    result dict. This is the function the skill's plugin code calls
    when the user invokes the ai-code-doctor skill on a target.

    The result includes both per-file findings and an aggregate summary
    across all analyzed files. The summary counts are computed only
    from files that were actually analyzed (``skipped=False``); skipped
    files are counted separately in ``summary.skipped_files``.

    Args:
        path: The input path (file or directory).

    Returns:
        A dict with keys ``input``, ``file_count``, ``files`` (list of
        per-file results), and ``summary`` (aggregate counts). If the
        path does not resolve to any Python files, the result includes
        an ``error`` field describing why.
    """
    files = collect_files(path)
    if not files:
        return {
            "input": path,
            "error": "no_python_files_found" if os.path.isdir(path) else "path_not_found",
            "file_count": 0,
            "files": [],
            "summary": {
                "total_functions": 0,
                "max_complexity": 0,
                "duplicate_groups": 0,
                "hub_functions": 0,
                "io_db_points": 0,
            },
        }

    per_file = [analyze_file(f) for f in files]

    # Aggregate counts. Only files that were not skipped contribute to
    # the per-metric sums; skipped files are tallied separately.
    total_functions = sum(
        f.get("function_count", 0) for f in per_file if not f.get("skipped")
    )
    max_complexity = max(
        (f.get("max_complexity", 0) for f in per_file if not f.get("skipped")),
        default=0,
    )
    duplicate_groups = sum(len(f.get("duplicates", [])) for f in per_file)
    hub_functions = sum(len(f.get("hubs", [])) for f in per_file)
    io_db_points = sum(len(f.get("io_db_points", [])) for f in per_file)
    skipped = sum(1 for f in per_file if f.get("skipped"))

    return {
        "input": path,
        "file_count": len(files),
        "files": per_file,
        "summary": {
            "total_functions": total_functions,
            "max_complexity": max_complexity,
            "duplicate_groups": duplicate_groups,
            "hub_functions": hub_functions,
            "io_db_points": io_db_points,
            "skipped_files": skipped,
        },
    }


def main(argv):
    """CLI entry point.

    Reads a single argument (file or directory path), runs
    :func:`analyze`, and prints the result as JSON to stdout. The
    function never raises: any exception during analysis is caught and
    reported as an ``error`` field in the JSON output, so the caller
    (typically an LLM-driven skill harness) always receives parseable
    JSON.

    Args:
        argv: The ``sys.argv`` list. ``argv[0]`` is the script name;
            ``argv[1]`` is the input path.

    Returns:
        0 on success (including analysis with errors), 1 on usage
        error (missing argument).
    """
    if len(argv) < 2:
        print(json.dumps({
            "error": "usage: analyze.py <file_or_dir>",
            "files": [],
        }))
        return 1

    path = argv[1]
    try:
        result = analyze(path)
    except Exception as e:
        result = {
            "input": path,
            "error": "fatal: %s" % str(e),
            "file_count": 0,
            "files": [],
            "summary": {},
        }

    # ensure_ascii=False preserves any non-ASCII characters in source
    # snippets or file paths; indent=2 makes the output readable when
    # the LLM inspects it directly.
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
