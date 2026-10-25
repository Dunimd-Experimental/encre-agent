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

from __future__ import annotations

"""Tests for ``_sanitize_tool_groups`` -- the pause/resume tool-closure fix.

A cancelled turn can leave a message list in a shape the LLM backend rejects
with a 400: a declared tool call with no result, a result with no declaration,
or ids that were never persisted. Each test here builds one such shape and
asserts sanitize closes it into a sequence the backend will accept.
"""

import unittest

from encre.compact.engine import _sanitize_tool_groups


def _assistant(tool_call_ids: list[str], text: str = "") -> dict:
    """Build an assistant message declaring one tool call per supplied id.

    Each fabricated tool call uses the dummy function name ``"x"`` with empty
    JSON arguments, which is all sanitize needs in order to key results by id.
    The optional ``text`` becomes the message ``content``.
    """
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {"id": tid, "type": "function", "function": {"name": "x", "arguments": "{}"}}
            for tid in tool_call_ids
        ],
    }


def _tool(tid: str, content: str = "ok") -> dict:
    """Build a single tool-result message answering tool call ``tid``.

    The ``content`` defaults to ``"ok"`` so a real result can be told apart
    from the tombstone text that sanitize injects for missing results.
    """
    return {"role": "tool", "tool_call_id": tid, "content": content}


class SanitizeToolGroupsTest(unittest.TestCase):
    """Cover the message-list shapes a cancelled turn can leave behind.

    Nine scenarios are checked: complete groups, incomplete groups with
    tombstones, leading orphans, orphans after a complete group, empty ids,
    two incomplete groups, idempotency, and input immutability. Each one
    asserts that the repaired sequence is one the backend will accept.
    """

    def test_verify_complete_group_unchanged(self):
        """A complete assistant/tool group passes through untouched.

        The assistant declares A and B and both results are present, so the
        roles and tool_call_ids survive in order with no correction applied.
        """
        msgs = [_assistant(["A", "B"]), _tool("A"), _tool("B")]
        out = _sanitize_tool_groups(msgs)
        self.assertEqual([m["role"] for m in out], ["assistant", "tool", "tool"])
        self.assertEqual(out[1]["tool_call_id"], "A")
        self.assertEqual(out[2]["tool_call_id"], "B")

    def test_verify_incomplete_group_gets_tombstone_not_dropped(self):
        """An incomplete group is kept and closed with a tombstone.

        The assistant declares A and B but only A's result landed. The group
        must not be dropped: the real A result has to survive the repair, and
        B receives a tombstone so the model still sees the cancelled call.
        """
        # Cancel mid-turn: assistant declared A and B, only A's result landed.
        msgs = [_assistant(["A", "B"]), _tool("A")]
        out = _sanitize_tool_groups(msgs)
        # The group is kept (not dropped) and B gets a tombstone, so the
        # good A result survives the repair.
        roles = [m["role"] for m in out]
        self.assertEqual(roles, ["assistant", "tool", "tool"])
        self.assertEqual(out[1]["tool_call_id"], "A")
        self.assertEqual(out[1]["content"], "ok")
        self.assertEqual(out[2]["tool_call_id"], "B")
        self.assertIn("not persisted", out[2]["content"])

    def test_verify_orphan_tool_result_dropped(self):
        """An orphan tool result with no declaration is dropped.

        No assistant declared GHOST, so the result has no corresponding
        tool_call and would break the message schema if it were kept.
        """
        # A tool result whose id no assistant declared -- the streaming-cancel
        # case where the assistant message was never persisted but a result was.
        msgs = [
            {"role": "user", "content": "hi"},
            _tool("GHOST"),  # No assistant declared GHOST.
            {"role": "user", "content": "again"},
        ]
        out = _sanitize_tool_groups(msgs)
        roles = [m["role"] for m in out]
        self.assertNotIn("tool", roles)
        self.assertEqual(roles, ["user", "user"])

    def test_verify_orphan_after_complete_group_dropped(self):
        """An orphan result after a complete group is dropped.

        The A,B group is intact; the stray C from an earlier turn has no
        declaration in this sequence, so only the A,B group survives.
        """
        # Complete A,B group followed by a stray C result from a prior turn.
        msgs = [_assistant(["A", "B"]), _tool("A"), _tool("B"), _tool("C")]
        out = _sanitize_tool_groups(msgs)
        roles = [m["role"] for m in out]
        self.assertEqual(roles, ["assistant", "tool", "tool"])
        ids = [m.get("tool_call_id") for m in out if m["role"] == "tool"]
        self.assertEqual(ids, ["A", "B"])

    def test_verify_leading_orphan_tools_dropped(self):
        """Leading orphan results before any assistant are dropped.

        X and Y appear before the assistant declares anything, so they have no
        context to attach to; only the valid assistant/tool pair survives.
        """
        msgs = [_tool("X"), _tool("Y"), _assistant(["A"]), _tool("A")]
        out = _sanitize_tool_groups(msgs)
        roles = [m["role"] for m in out]
        self.assertEqual(roles, ["assistant", "tool"])

    def test_verify_assistant_with_no_id_tool_calls_dropped(self):
        """An assistant with empty-id tool calls is dropped entirely.

        An empty id is invalid, so both the assistant and the tool result that
        cannot be matched to any declaration are removed, leaving [].
        """
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ]},
            _tool("whatever"),
        ]
        out = _sanitize_tool_groups(msgs)
        self.assertEqual(out, [])

    def test_verify_two_groups_both_incomplete(self):
        """Two incomplete groups each get a tombstone for their missing result.

        Each group is closed independently, so all four ids appear in the
        output and B and D carry the tombstone content.
        """
        msgs = [
            _assistant(["A", "B"]), _tool("A"),  # B's result never landed.
            _assistant(["C", "D"]), _tool("C"),  # D's result never landed.
        ]
        out = _sanitize_tool_groups(msgs)
        # Both groups are kept, each closed with a tombstone for its missing id.
        ids = [m.get("tool_call_id") for m in out if m["role"] == "tool"]
        self.assertEqual(ids, ["A", "B", "C", "D"])
        tombstoned = [m for m in out if m["role"] == "tool" and "not persisted" in m["content"]]
        self.assertEqual(len(tombstoned), 2)
        tomb_ids = {m["tool_call_id"] for m in tombstoned}
        self.assertEqual(tomb_ids, {"B", "D"})

    def test_verify_idempotent(self):
        """Running sanitize twice produces identical output.

        The first pass closes the incomplete group; the second pass must be a
        no-op on the already-correct list.
        """
        msgs = [_assistant(["A", "B"]), _tool("A")]
        once = _sanitize_tool_groups(msgs)
        twice = _sanitize_tool_groups(once)
        self.assertEqual(once, twice)

    def test_verify_does_not_mutate_input(self):
        """Sanitize returns a new list instead of mutating the caller's.

        The input is captured by shallow copy before the call, then compared
        against the original to prove the caller's data was left alone.
        """
        msgs = [_assistant(["A"]), _tool("A")]
        original = [dict(m) for m in msgs]
        _sanitize_tool_groups(msgs)
        self.assertEqual(msgs, original)


if __name__ == "__main__":
    unittest.main()
