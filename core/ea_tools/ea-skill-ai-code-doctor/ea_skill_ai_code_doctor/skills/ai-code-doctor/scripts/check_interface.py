#!/usr/bin/env python3
from __future__ import annotations

"""Check that an optimized Python file keeps the original public interface."""

import ast
import json
import sys


def _read_tree(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        return ast.parse(handle.read(), filename=path)


def _expression_text(node):
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except AttributeError:
        return ast.dump(node, annotate_fields=False, include_attributes=False)


def _argument_text(arg):
    annotation = _expression_text(arg.annotation)
    return arg.arg if annotation is None else "%s: %s" % (arg.arg, annotation)


def _signature(node):
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    positional_defaults = [None] * (len(positional) - len(args.defaults))
    positional_defaults.extend(args.defaults)
    parts = []
    for index, (arg, default) in enumerate(zip(positional, positional_defaults)):
        text = _argument_text(arg)
        if default is not None:
            text += "=" + _expression_text(default)
        parts.append(text)
        if args.posonlyargs and index + 1 == len(args.posonlyargs):
            parts.append("/")
    if args.vararg is not None:
        parts.append("*" + _argument_text(args.vararg))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        text = _argument_text(arg)
        if default is not None:
            text += "=" + _expression_text(default)
        parts.append(text)
    if args.kwarg is not None:
        parts.append("**" + _argument_text(args.kwarg))
    result = "(" + ", ".join(parts) + ")"
    if node.returns is not None:
        result += " -> " + _expression_text(node.returns)
    return ("async " if isinstance(node, ast.AsyncFunctionDef) else "") + result


def _interface(path):
    tree = _read_tree(path)
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.name] = {
                "kind": "function",
                "signature": _signature(node),
                "line": node.lineno,
            }
        elif isinstance(node, ast.ClassDef):
            result[node.name] = {"kind": "class", "signature": None, "line": node.lineno}
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result["%s.%s" % (node.name, member.name)] = {
                        "kind": "method",
                        "signature": _signature(member),
                        "line": member.lineno,
                    }
    return result


def compare_interfaces(original_path, optimized_path):
    original = _interface(original_path)
    optimized = _interface(optimized_path)
    missing = sorted(set(original) - set(optimized))
    added = sorted(set(optimized) - set(original))
    changed = []
    for name in sorted(set(original) & set(optimized)):
        before = original[name]
        after = optimized[name]
        if before["kind"] != after["kind"] or before["signature"] != after["signature"]:
            changed.append({
                "name": name,
                "before": before,
                "after": after,
            })
    return {
        "compatible": not missing and not changed,
        "missing": [{"name": name, **original[name]} for name in missing],
        "changed": changed,
        "added": [{"name": name, **optimized[name]} for name in added],
    }


def main(argv):
    if len(argv) != 3:
        print(json.dumps({"error": "usage: check_interface.py <original.py> <optimized.py>"}))
        return 2
    try:
        result = compare_interfaces(argv[1], argv[2])
    except (OSError, SyntaxError) as error:
        result = {"compatible": False, "error": "%s: %s" % (type(error).__name__, error)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["compatible"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
