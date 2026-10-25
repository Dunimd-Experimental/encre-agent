#!/usr/bin/env python
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

"""Offline behaviour evals for the Encre Agent Harness.

Every task runs the real loop against a *scripted* backend, so the suite is
deterministic, network-free, and fast enough for every pull request.  It is the
harness's regression baseline: if a refactor changes turn semantics, recovery
behaviour, tool dispatch, or compaction, a task here fails.

Usage::

    python harness/evals/run_evals.py                  # run, compare to baseline
    python harness/evals/run_evals.py --update-baseline
    python harness/evals/run_evals.py --category tool --verbose
    python harness/evals/run_evals.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

HARNESS_ROOT = Path(__file__).resolve().parents[1]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

# The harness must never touch the developer's real data directory.
os.environ.setdefault(
    "ENCRE_DATA_DIR", tempfile.mkdtemp(prefix="encre-evals-")
)

from encre.backends.base import BaseBackend  # noqa: E402
from encre.config import EncreConfig  # noqa: E402
from encre.sdk import EncreSDK  # noqa: E402
from encre.tools.base import EncreTool  # noqa: E402
from encre.utils.types import (  # noqa: E402
    BackendError,
    create_backend_finish,
    create_backend_text,
    create_backend_tool_call,
)

EVALS_DIR = Path(__file__).resolve().parent
TASKS_PATH = EVALS_DIR / "tasks.jsonl"
BASELINE_PATH = EVALS_DIR / "baseline.json"


# --------------------------------------------------------------------------- #
# Tools used by the eval tasks
# --------------------------------------------------------------------------- #
class EchoTool(EncreTool):
    name = "echo"
    description = "Echo the given text back, prefixed with 'echo:'."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, text: str = "", **_kwargs: Any) -> str:
        return f"echo:{text}"


class StrictEchoTool(EncreTool):
    name = "strict_echo"
    description = "Echo text; fails when 'text' is missing or empty."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, text: str = "", **_kwargs: Any) -> str:
        if not text:
            raise ValueError("strict_echo requires a non-empty 'text'")
        return f"strict:{text}"


class BoomTool(EncreTool):
    name = "boom"
    description = "Always raises, to exercise tool-error recovery."
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def execute(self, **_kwargs: Any) -> str:
        raise RuntimeError("tool exploded")


class BigOutputTool(EncreTool):
    name = "big_output"
    description = "Return a large deterministic payload."
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def execute(self, **_kwargs: Any) -> str:
        return "BEGIN-" + ("0123456789" * 2000) + "-END"


ALL_TOOLS = [EchoTool(), StrictEchoTool(), BoomTool(), BigOutputTool()]

#: A realistic-enough compaction summary (the engine rejects very short ones).
DEFAULT_SUMMARY = (
    "<summary>\n"
    "## Session intent\nThe user asked the agent to exercise the harness eval suite.\n"
    "## Completed work\nThe scripted backend replayed every turn and tool call.\n"
    "## Pending work\nNone; the final response confirms completion.\n"
    "## Key facts\n- eval marker: compact-ok\n- tool results were preserved\n"
    "</summary>"
)


# --------------------------------------------------------------------------- #
# Scripted backend
# --------------------------------------------------------------------------- #
class ScriptedBackend(BaseBackend):
    """A backend that replays a JSON script instead of calling a model."""

    #: Marker in the memory-importance classifier request (it shares the loop
    #: backend but must answer with a bare decimal, never consume a script step).
    CLASSIFIER_MARKER = "PERSISTENT long-term memory"
    #: Marker present in the compaction summarisation request.
    COMPACT_MARKER = "summarising an AI agent conversation"

    def __deepcopy__(self, memo: dict[Any, Any]):
        # Any deep copy is a helper request (memory classifier, compaction
        # pipeline).  Sharing the script with the real backend would burn a
        # scripted step on a side-channel call, so copies answer a constant.
        return ScriptedBackend(
            [],
            window=self.window,
            default_text="0.0",
            supports_tools=False,
        )

    def __init__(
        self,
        script: list[dict[str, Any]],
        *,
        window: int,
        default_text: str,
        supports_tools: bool = True,
        compaction_text: str | None = None,
    ) -> None:
        self.script = list(script)
        self.calls = 0
        self.window = window
        self.default_text = default_text
        self._supports_tools = supports_tools
        self.compaction_text = compaction_text or DEFAULT_SUMMARY

    @staticmethod
    def _matches(messages: list[dict[str, Any]], marker: str) -> bool:
        """Return True when a whole conversation contains ``marker``."""
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and marker in content:
                return True
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str) and marker in text:
                            return True
        return False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,  # noqa: ARG002 - mirrors BaseBackend
        **kwargs: Any,  # noqa: ARG002 - mirrors BaseBackend
    ):
        self.calls += 1

        if self._matches(messages, self.CLASSIFIER_MARKER):
            yield create_backend_text("0.0")
            yield create_backend_finish("stop")
            return

        if self._matches(messages, self.COMPACT_MARKER):
            yield create_backend_text(self.compaction_text)
            yield create_backend_finish("stop")
            return

        step = self.script.pop(0) if self.script else {"text": self.default_text}

        if "error" in step:
            yield BackendError(
                error=step["error"],
                retryable=step.get("retryable", True),
                code=step.get("code", "server_error"),
            )
            return

        if "tools" in step:
            for index, spec in enumerate(step["tools"]):
                arguments = spec.get("arguments", {})
                if "arg_repeat" in spec:
                    repeat = spec["arg_repeat"]
                    arguments = dict(arguments)
                    arguments[repeat["field"]] = repeat["chunk"] * repeat["times"]
                yield create_backend_tool_call(
                    spec.get("id", f"call_{self.calls}_{index}"),
                    spec["name"],
                    json.dumps(arguments),
                )
            yield create_backend_finish("tool_calls")
            return

        text = step.get("text", "")
        if "text_repeat" in step:
            repeat = step["text_repeat"]
            text = repeat["chunk"] * repeat["times"] + repeat.get("suffix", "")
        if text:
            yield create_backend_text(text)
        yield create_backend_finish(step.get("finish", "stop"))

    def supports_tool_calling(self) -> bool:
        return self._supports_tools

    def context_window_size(self) -> int:
        return self.window


# --------------------------------------------------------------------------- #
# Task execution
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    task_id: str
    category: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    backend_calls: int = 0
    text_len: int = 0
    elapsed_ms: int = 0
    events: list[str] = field(default_factory=list)
    error: str = ""


def _build_prompt(task: dict[str, Any]) -> str:
    prompt = task.get("prompt", "hello")
    repeat = task.get("prompt_repeat")
    if repeat:
        prompt += " " + repeat["chunk"] * repeat["times"]
    return prompt


async def _consume(loop: Any, prompt: str, timeout_s: float) -> dict[str, Any]:
    events: list[str] = []
    text_parts: list[str] = []
    tool_results: list[str] = []
    finish_reason: str | None = None
    finish_error: str | None = None

    async def _run() -> None:
        nonlocal finish_reason, finish_error
        async for event in loop.run(prompt):
            name = type(event).__name__
            events.append(name)
            if name == "TextDelta":
                text_parts.append(event.text)
            elif name == "ToolResult":
                tool_results.append(event.content)
            elif name == "Finish":
                reason = event.reason
                finish_reason = str(getattr(reason, "value", reason))
                finish_error = event.error

    await asyncio.wait_for(_run(), timeout=timeout_s)
    return {
        "events": events,
        "text": "".join(text_parts),
        "tool_results": tool_results,
        "finish_reason": finish_reason,
        "finish_error": finish_error,
    }


def _check(task: dict[str, Any], result: Result, observed: dict[str, Any]) -> None:
    expect = task.get("expect", {})
    text = observed["text"]
    events = observed["events"]
    tool_results = observed["tool_results"]

    for needle in expect.get("final_text_contains", []):
        if needle not in text:
            result.failures.append(f"final text missing {needle!r} (got {text[:200]!r})")

    for needle in expect.get("final_text_not_contains", []):
        if needle in text:
            result.failures.append(f"final text unexpectedly contains {needle!r}")

    for event in expect.get("events_include", []):
        if event not in events:
            result.failures.append(f"missing event {event!r} (got {events})")

    for event in expect.get("events_exclude", []):
        if event in events:
            result.failures.append(f"unexpected event {event!r}")

    for needle in expect.get("tool_results_include", []):
        if not any(needle in item for item in tool_results):
            result.failures.append(
                f"no tool result contains {needle!r} (got {tool_results})"
            )

    calls = expect.get("backend_calls")
    if calls:
        low, high = calls
        if not low <= result.backend_calls <= high:
            result.failures.append(
                f"backend calls {result.backend_calls} outside [{low}, {high}]"
            )

    if "min_text_len" in expect and len(text) < expect["min_text_len"]:
        result.failures.append(
            f"final text too short: {len(text)} < {expect['min_text_len']}"
        )

    if "finish_reason" in expect and observed["finish_reason"] != expect["finish_reason"]:
        result.failures.append(
            f"finish reason {observed['finish_reason']!r} != {expect['finish_reason']!r}"
        )


async def run_task(task: dict[str, Any], *, timeout_s: float = 90.0) -> Result:
    result = Result(
        task_id=task["id"],
        category=task.get("category", "uncategorised"),
        passed=False,
    )
    started = time.perf_counter()
    try:
        cfg = EncreConfig(model="eval-model", backend_type="openai")
        sdk = EncreSDK.from_config(
            cfg,
            tools=ALL_TOOLS,
            register_builtins=False,
        )
        backend = ScriptedBackend(
            task.get("script", []),
            window=int(task.get("window", 200000)),
            default_text=task.get("default_text", "done"),
            supports_tools=bool(task.get("supports_tools", True)),
        )
        sdk.loop.backend = backend

        observed = await _consume(sdk.loop, _build_prompt(task), timeout_s)
        result.backend_calls = backend.calls
        result.text_len = len(observed["text"])
        result.events = observed["events"]
        _check(task, result, observed)
        result.passed = not result.failures
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.failures.append(result.error)
    finally:
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


# --------------------------------------------------------------------------- #
# Baseline + reporting
# --------------------------------------------------------------------------- #
def load_tasks(path: Path = TASKS_PATH) -> list[dict[str, Any]]:
    tasks = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tasks.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return tasks


def load_baseline() -> dict[str, Any]:
    if not BASELINE_PATH.exists():
        return {"version": 1, "tasks": {}}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def save_baseline(results: list[Result]) -> None:
    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks": {
            r.task_id: {
                "passed": r.passed,
                "backend_calls": r.backend_calls,
                "text_len": r.text_len,
                "elapsed_ms": r.elapsed_ms,
            }
            for r in results
        },
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def compare_to_baseline(results: list[Result], baseline: dict[str, Any]) -> list[str]:
    regressions: list[str] = []
    tasks = baseline.get("tasks", {})
    for result in results:
        previous = tasks.get(result.task_id)
        if not previous:
            continue
        if previous.get("passed") and not result.passed:
            regressions.append(f"{result.task_id}: passed in baseline, now fails")
        previous_calls = previous.get("backend_calls", 0)
        if previous_calls and result.backend_calls > previous_calls * 2:
            regressions.append(
                f"{result.task_id}: backend calls {result.backend_calls} "
                f"more than doubled (baseline {previous_calls})"
            )
    return regressions


async def run_all(tasks: list[dict[str, Any]]) -> list[Result]:
    results = []
    for task in tasks:
        results.append(await run_task(task))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", help="only run this category")
    parser.add_argument("--id", dest="task_id", help="only run this task id")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    tasks = load_tasks()
    if args.category:
        tasks = [t for t in tasks if t.get("category") == args.category]
    if args.task_id:
        tasks = [t for t in tasks if t["id"] == args.task_id]
    if not tasks:
        print("no tasks matched", file=sys.stderr)
        return 2

    results = asyncio.run(run_all(tasks))
    failures = [r for r in results if not r.passed]

    baseline = load_baseline()
    regressions = compare_to_baseline(results, baseline)

    if args.update_baseline:
        save_baseline(results)

    if args.json:
        print(
            json.dumps(
                {
                    "total": len(results),
                    "passed": len(results) - len(failures),
                    "failed": len(failures),
                    "regressions": regressions,
                    "results": [r.__dict__ for r in results],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for result in results:
            mark = "PASS" if result.passed else "FAIL"
            print(
                f"[{mark}] {result.task_id:<34} "
                f"calls={result.backend_calls:<2} "
                f"chars={result.text_len:<6} {result.elapsed_ms:>5}ms"
            )
            if result.failures and (args.verbose or not result.passed):
                for failure in result.failures:
                    print(f"       - {failure}")
            if args.verbose and result.events:
                print(f"       events: {result.events}")
        print()
        print(f"{len(results) - len(failures)}/{len(results)} passed")
        if regressions:
            print("\nREGRESSIONS vs baseline:")
            for regression in regressions:
                print(f"  - {regression}")
        if args.update_baseline:
            print(f"\nbaseline written to {BASELINE_PATH}")

    return 1 if failures or regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
