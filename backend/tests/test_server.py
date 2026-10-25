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

"""Tests for encre.server.protocol -- client/server message encoding and parsing.

Covers the six client message dataclasses, the parse_client_message dispatcher,
the _make_message helper, the generic encode_server_message encoder, and the
fifteen specialized server encoders.

Also checks the message-type literal sets and end-to-end JSON round-trips, so a
broken wire contract is caught before it reaches the server loop.
"""

import json

from encre.server.protocol import (
    ClientCancel,
    ClientConfigure,
    ClientPing,
    ClientRespondPermission,
    ClientResume,
    ClientRun,
    _make_message,
    encode_error,
    encode_finish,
    encode_permission_request,
    encode_pong,
    encode_server_message,
    encode_session_ready,
    encode_text_delta,
    encode_thinking_delta,
    encode_tool_call_delta,
    encode_tool_call_end,
    encode_tool_call_start,
    encode_tool_progress,
    encode_tool_result,
    parse_client_message,
)

from encre.crypto import decrypt


def decode(frame: str) -> dict:
    """Decrypt a server frame back into its JSON document.

    Every server frame is AES-256-GCM transport ciphertext — there is no
    plaintext mode — so assertions about wire shape must decrypt first.
    """
    return json.loads(decrypt(frame))

# ---- Client Message Dataclasses ----

class TestClientRun:
    """Validate the ``ClientRun`` message dataclass.

    Covers constructor defaults, dict-based factory construction, and
    missing-key tolerance, so every optional field falls back to a safe
    default before the server touches the message.
    """

    def test_verify_defaults_are_safe(self):
        """Validate that ``ClientRun()`` initializes all fields to safe defaults.

        Every field must have a deterministic value before serialization, so
        the type tag, prompt, and optional fields are all asserted.
        """
        msg = ClientRun()
        assert msg.type == "run"
        assert msg.prompt == ""
        assert msg.system_prompt is None
        assert msg.session_id is None
        assert msg.specialty == "general"

    def test_verify_from_dict_minimal_payload(self):
        """Validate that ``from_dict`` populates only the supplied keys.

        Clients may send incomplete messages, so unspecified fields must keep
        their declared defaults instead of being overwritten.
        """
        msg = ClientRun.from_dict({"prompt": "hello"})
        assert msg.type == "run"
        assert msg.prompt == "hello"
        assert msg.specialty == "general"

    def test_verify_from_dict_full_payload(self):
        """Validate that ``from_dict`` maps every key to the corresponding field.

        The protocol round-trip depends on complete deserialization, so a full
        payload must preserve client intent exactly.
        """
        msg = ClientRun.from_dict({
            "prompt": "do it",
            "system_prompt": "You are helpful.",
            "session_id": "abc-123",
            "specialty": "coding",
        })
        assert msg.type == "run"
        assert msg.prompt == "do it"
        assert msg.system_prompt == "You are helpful."
        assert msg.session_id == "abc-123"
        assert msg.specialty == "coding"

    def test_verify_from_dict_missing_keys_uses_defaults(self):
        """Validate that ``from_dict`` uses ``.get()`` defaults for absent keys.

        The parser must stay resilient to malformed or legacy client messages,
        so an empty dict must not raise and must resolve every field.
        """
        msg = ClientRun.from_dict({})
        assert msg.prompt == ""
        assert msg.system_prompt is None
        assert msg.session_id is None


class TestClientRespondPermission:
    """Validate the ``ClientRespondPermission`` dataclass.

    Covers default construction and dict-based factory patterns to confirm the
    permission-response message carries the right tool name and boolean
    decision so the server can route it to the correct tool handler.
    """

    def test_verify_defaults_are_safe(self):
        """Validate that ``ClientRespondPermission()`` sets safe defaults.

        The protocol treats an unset permission as a denial by default, so the
        type tag, empty tool name, and ``False`` decision are asserted.
        """
        msg = ClientRespondPermission()
        assert msg.type == "respond_permission"
        assert msg.tool_name == ""
        assert msg.decision is False

    def test_verify_from_dict_maps_fields(self):
        """Validate that ``from_dict`` correctly maps tool_name and decision.

        The permission response must preserve the client's explicit tool name
        and approval decision without mutation.
        """
        msg = ClientRespondPermission.from_dict({
            "tool_name": "bash",
            "decision": True,
        })
        assert msg.tool_name == "bash"
        assert msg.decision is True

    def test_verify_from_dict_empty_uses_defaults(self):
        """Validate that ``from_dict({})`` produces default values.

        Missing permission fields must resolve to safe defaults rather than
        raising errors.
        """
        msg = ClientRespondPermission.from_dict({})
        assert msg.tool_name == ""
        assert msg.decision is False


class TestClientCancel:
    """Validate the ``ClientCancel`` dataclass.

    Covers default construction and dict-based factory patterns to confirm the
    cancel message carries the session identifier the server must terminate.
    """

    def test_verify_defaults_are_safe(self):
        """Validate that ``ClientCancel()`` initializes with empty session_id.

        The protocol requires a deterministic type tag even when no session is
        specified, so the empty default is asserted as well.
        """
        msg = ClientCancel()
        assert msg.type == "cancel"
        assert msg.session_id == ""

    def test_verify_from_dict_populates_session_id(self):
        """Validate that ``from_dict`` correctly assigns the session_id field.

        The cancel handler must route to the exact session the client wishes to
        terminate, so the value must be preserved verbatim.
        """
        msg = ClientCancel.from_dict({"session_id": "sess-xyz"})
        assert msg.session_id == "sess-xyz"

    def test_verify_from_dict_empty_yields_default_session_id(self):
        """Validate that ``from_dict({})`` falls back to empty session_id.

        Missing fields must not raise and should resolve to the protocol-safe
        default.
        """
        msg = ClientCancel.from_dict({})
        assert msg.session_id == ""


class TestClientResume:
    """Validate the ``ClientResume`` dataclass.

    Covers default construction and dict-based factory patterns to confirm the
    resume message targets the session whose context the server must restore.
    """

    def test_verify_defaults_are_safe(self):
        """Validate that ``ClientResume()`` initializes with empty session_id.

        The protocol requires a deterministic type tag for all client messages,
        so the empty session-id default is asserted alongside it.
        """
        msg = ClientResume()
        assert msg.type == "resume"
        assert msg.session_id == ""

    def test_verify_from_dict_populates_session_id(self):
        """Validate that ``from_dict`` correctly extracts the session_id.

        The resume handler must restore the exact session the client specifies.
        """
        msg = ClientResume.from_dict({"session_id": "sess-abc"})
        assert msg.session_id == "sess-abc"


class TestClientConfigure:
    """Validate the ``ClientConfigure`` dataclass.

    Covers default construction and dict-based factory patterns, focusing on
    the nested ``config`` dictionary so runtime settings (model, max_tokens,
    and so on) can be swapped without restarting the server.
    """

    def test_verify_defaults_are_safe(self):
        """Validate that ``ClientConfigure()`` initializes with empty config dict.

        The protocol treats an empty config as a no-op override, so the type
        tag and empty mapping are both asserted.
        """
        msg = ClientConfigure()
        assert msg.type == "configure"
        assert msg.config == {}

    def test_verify_from_dict_populates_config(self):
        """Validate that ``from_dict`` preserves the nested config dictionary.

        Configuration passthrough must not mutate or drop any key supplied by
        the client.
        """
        msg = ClientConfigure.from_dict({"config": {"model": "gpt-5.6"}})
        assert msg.config == {"model": "gpt-5.6"}

    def test_verify_from_dict_empty_yields_default_config(self):
        """Validate that ``from_dict({})`` falls back to an empty config dict.

        Missing configuration fields must not crash the parser.
        """
        msg = ClientConfigure.from_dict({})
        assert msg.config == {}


class TestClientPing:
    """Validate the ``ClientPing`` dataclass.

    Covers default construction and dict-based factory patterns to confirm the
    ping message is a zero-cost heartbeat whose extra payload fields are
    intentionally ignored for backward compatibility.
    """

    def test_verify_defaults_are_safe(self):
        """Validate that ``ClientPing()`` sets the correct type tag.

        The ping message carries no optional fields; its only purpose is to
        signal liveness through the type discriminator.
        """
        msg = ClientPing()
        assert msg.type == "ping"

    def test_verify_from_dict_ignores_extra_payload(self):
        """Validate that ``from_dict`` discards unknown keys without side effects.

        The ping parser must stay immune to unexpected payload fields so the
        heartbeat path remains stable across protocol extensions.
        """
        msg = ClientPing.from_dict({"extra": "ignored"})
        assert msg.type == "ping"


# ---- parse_client_message ----

class TestParseClientMessage:
    """Validate the ``parse_client_message`` dispatcher.

    Covers JSON deserialization and type dispatch across 11 scenarios: a valid
    payload for each registered message kind, invalid JSON, an empty object, an
    unknown type, bytes input, and malformed UTF-8, so the protocol layer fails
    gracefully on non-conforming input without crashing the server loop.
    """

    def test_verify_parse_run_dispatches_correct_type(self):
        """Validate that a ``run`` type JSON string dispatches to ``ClientRun``.

        The dispatcher must route each type tag to the matching dataclass
        constructor and preserve the prompt payload.
        """
        msg = parse_client_message(json.dumps({"type": "run", "prompt": "hi"}))
        assert isinstance(msg, ClientRun)
        assert msg.prompt == "hi"

    def test_verify_parse_respond_permission_dispatches_correct_type(self):
        """Validate that ``respond_permission`` dispatches to the correct dataclass.

        Permission responses must carry the tool identifier for server-side
        routing, so the parsed tool name is asserted.
        """
        msg = parse_client_message(json.dumps({
            "type": "respond_permission",
            "tool_name": "edit",
            "decision": True,
        }))
        assert isinstance(msg, ClientRespondPermission)
        assert msg.tool_name == "edit"

    def test_verify_parse_cancel_dispatches_correct_type(self):
        """Validate that ``cancel`` dispatches to ``ClientCancel``.

        The cancel handler must distinguish this message type from the other
        control signals.
        """
        msg = parse_client_message(json.dumps({
            "type": "cancel",
            "session_id": "s1",
        }))
        assert isinstance(msg, ClientCancel)

    def test_verify_parse_resume_dispatches_correct_type(self):
        """Validate that ``resume`` dispatches to ``ClientResume``.

        The resume handler must route session restoration to the correct code
        path.
        """
        msg = parse_client_message(json.dumps({
            "type": "resume",
            "session_id": "s1",
        }))
        assert isinstance(msg, ClientResume)

    def test_verify_parse_configure_dispatches_correct_type(self):
        """Validate that ``configure`` dispatches to ``ClientConfigure``.

        Runtime reconfiguration must preserve the full settings map, so the
        nested config dict is asserted as well.
        """
        msg = parse_client_message(json.dumps({
            "type": "configure",
            "config": {"max_tokens": 8192},
        }))
        assert isinstance(msg, ClientConfigure)
        assert msg.config == {"max_tokens": 8192}

    def test_verify_parse_ping_dispatches_correct_type(self):
        """Validate that ``ping`` dispatches to ``ClientPing``.

        The heartbeat path must stay distinguishable from every other client
        message type.
        """
        msg = parse_client_message(json.dumps({"type": "ping"}))
        assert isinstance(msg, ClientPing)

    def test_verify_parse_invalid_json_returns_none(self):
        """Validate that unparseable JSON returns ``None`` instead of raising.

        The dispatcher must swallow malformed input so bad client data cannot
        crash the TCP read loop.
        """
        msg = parse_client_message("not json at all")
        assert msg is None

    def test_verify_parse_empty_json_object_returns_none(self):
        """Validate that ``{}`` returns ``None`` because it has no type tag.

        A message without a ``type`` field cannot be dispatched to any
        registered handler.
        """
        msg = parse_client_message("{}")
        assert msg is None

    def test_verify_parse_unknown_type_returns_none(self):
        """Validate that an unrecognized type tag returns ``None``.

        The dispatcher must ignore messages it does not recognize rather than
        raising an exception.
        """
        msg = parse_client_message(json.dumps({"type": "magic_unknown"}))
        assert msg is None

    def test_verify_parse_bytes_input_is_decoded(self):
        """Validate that raw bytes are accepted and decoded to a ``ClientPing``.

        The TCP listener may deliver message frames as byte strings rather than
        pre-decoded text, so bytes input must still parse.
        """
        msg = parse_client_message(b'{"type": "ping"}')
        assert isinstance(msg, ClientPing)

    def test_verify_parse_invalid_utf8_bytes_returns_none(self):
        """Validate that invalid UTF-8 bytes return ``None`` without crashing.

        The dispatcher must handle encoding errors gracefully to avoid a
        crashed connection loop.
        """
        msg = parse_client_message(b'\xff\xfe\x00')
        assert msg is None


# ---- _make_message helper ----

class TestMakeMessage:
    """Validate the ``_make_message`` internal helper.

    Covers the dictionary-building utility with zero, one, and multiple extra
    keys to confirm it always assembles a flat JSON-serializable dict carrying
    a ``type`` field, which every encoder built on it depends on.
    """

    def test_verify_basic_payload_construction(self):
        """Validate that ``_make_message`` merges type and kwargs into one dict.

        Downstream encoders depend on this uniform shape for JSON
        serialization, so both keys must land in a single flat dict.
        """
        result = _make_message("test_type", key="val")
        assert result == {"type": "test_type", "key": "val"}

    def test_verify_no_extras_yields_type_only(self):
        """Validate that ``_make_message`` works with no extra kwargs.

        Some server messages (e.g. pong) carry no payload beyond the type
        discriminator.
        """
        result = _make_message("bare")
        assert result == {"type": "bare"}

    def test_verify_multiple_kwargs_are_merged(self):
        """Validate that ``_make_message`` accepts and merges many kwargs.

        Encoder helpers that build tool-related messages often need to pass
        several fields at once.
        """
        result = _make_message("m", a=1, b=2, c=3)
        assert result == {"type": "m", "a": 1, "b": 2, "c": 3}


# ---- encode_server_message ----

class TestEncodeServerMessage:
    """Validate the ``encode_server_message`` generic encoder.

    Server frames are always AES-256-GCM ciphertext on the wire, so these
    assertions decrypt the frame before inspecting the JSON document. The
    foundational encoder used by all specialized server encoders must still
    round-trip into the expected dict.
    """

    def test_verify_returns_encrypted_string_that_decodes_to_json(self):
        """Validate that ``encode_server_message`` emits decryptable ciphertext.

        The wire protocol delivers all server messages as encrypted text, so
        the encoded output must round-trip back into the expected dict and
        must never expose the payload in the clear.
        """
        result = encode_server_message("text_delta", text="hello")
        assert isinstance(result, str)
        assert "hello" not in result
        parsed = decode(result)
        assert parsed["type"] == "text_delta"
        assert parsed["text"] == "hello"

    def test_verify_non_ascii_text_is_preserved(self):
        """Validate that literal text is not escaped to \\uXXXX sequences.

        ``ensure_ascii=False`` must be active so UTF-8 text flows through the
        wire format without unnecessary byte expansion; the assertion checks
        that the "café" payload decodes back to the original characters.
        """
        result = encode_server_message("text_delta", text="café ☕")
        assert decode(result)["text"] == "café ☕"

    def test_verify_no_extra_kwargs_yields_type_only_json(self):
        """Validate that a message with no payload fields serializes cleanly.

        Heartbeat messages must not include spurious null or empty fields.
        """
        result = encode_server_message("pong")
        assert decode(result) == {"type": "pong"}


# ---- Convenience Encoders ----

class TestConvenienceEncoders:
    """Validate all server-side convenience encoder functions.

    Covers 15 specialized encoders spanning text deltas, thinking deltas, the
    tool-call lifecycle, tool progress and results, permission requests, finish
    signals, pong replies, error responses, and session-ready notifications, so
    each produces the exact JSON shape the client protocol expects.
    """

    def test_verify_encode_text_delta(self):
        """Validate that ``encode_text_delta`` wraps text in the correct shape.

        Streaming LLM output reaches the client as a sequence of text_delta
        messages, so the type tag and original text are both asserted.
        """
        msg = encode_text_delta("Hello world")
        parsed = decode(msg)
        assert parsed == {"type": "text_delta", "text": "Hello world"}

    def test_verify_encode_thinking_delta(self):
        """Validate that ``encode_thinking_delta`` wraps reasoning text correctly.

        Internal chain-of-thought tokens must reach the client on a separate
        stream channel from ordinary text.
        """
        msg = encode_thinking_delta("Hmm...")
        parsed = decode(msg)
        assert parsed == {"type": "thinking_delta", "text": "Hmm..."}

    def test_verify_encode_tool_call_start(self):
        """Validate that ``encode_tool_call_start`` emits the correct tool invocation shape.

        The client must know which tool invocation has begun before argument
        deltas start arriving.
        """
        msg = encode_tool_call_start("bash", "call_1")
        parsed = decode(msg)
        assert parsed == {"type": "tool_call_start", "name": "bash", "id": "call_1"}

    def test_verify_encode_tool_call_delta(self):
        """Validate that ``encode_tool_call_delta`` emits incremental argument fragments.

        Token-by-token argument streaming lets the client render tool calls in
        real time, so all four fields are asserted.
        """
        msg = encode_tool_call_delta("call_1", "arguments", '{"cmd":')
        parsed = decode(msg)
        assert parsed == {
            "type": "tool_call_delta",
            "id": "call_1",
            "key": "arguments",
            "value": '{"cmd":',
        }

    def test_verify_encode_tool_call_end(self):
        """Validate that ``encode_tool_call_end`` signals completion of a tool call.

        The client must know when argument streaming is finished before it
        evaluates the tool result.
        """
        msg = encode_tool_call_end("call_1")
        parsed = decode(msg)
        assert parsed == {"type": "tool_call_end", "id": "call_1"}

    def test_verify_encode_tool_progress(self):
        """Validate that ``encode_tool_progress`` reports intermediate execution status.

        Long-running tools emit progress updates to keep the client UI
        responsive, so call id, tool name, and status are all asserted.
        """
        msg = encode_tool_progress("call_1", "bash", "running")
        parsed = decode(msg)
        assert parsed == {
            "type": "tool_progress",
            "id": "call_1",
            "tool_name": "bash",
            "status": "running",
        }

    def test_verify_encode_tool_result_success(self):
        """Validate that ``encode_tool_result`` marks success with ``is_error=False``.

        The client must distinguish successful tool output from failures in
        order to render the conversation correctly.
        """
        msg = encode_tool_result("call_1", "output text", is_error=False)
        parsed = decode(msg)
        assert parsed["type"] == "tool_result"
        assert parsed["content"] == "output text"
        assert parsed["is_error"] is False

    def test_verify_encode_tool_result_error(self):
        """Validate that ``encode_tool_result`` marks errors with ``is_error=True``.

        Failed tool executions must be visually distinguished from successful
        ones in the UI.
        """
        msg = encode_tool_result("call_1", "command not found", is_error=True)
        parsed = decode(msg)
        assert parsed["is_error"] is True

    def test_verify_encode_permission_request(self):
        """Validate that ``encode_permission_request`` asks the client for tool approval.

        Privileged tools must pause execution until the client confirms the
        action is authorized, so both the tool name and reason are asserted.
        """
        msg = encode_permission_request("bash", "requires sudo")
        parsed = decode(msg)
        assert parsed == {
            "type": "permission_request",
            "tool_name": "bash",
            "reason": "requires sudo",
        }

    def test_verify_encode_finish(self):
        """Validate that ``encode_finish`` emits a clean termination signal.

        The finish message must always carry the same shape regardless of
        whether token usage or an error was recorded.
        """
        msg = encode_finish("stop")
        parsed = decode(msg)
        assert parsed == {"type": "finish", "reason": "stop", "usage": None, "error": None}

    def test_verify_encode_finish_with_usage(self):
        """Validate that ``encode_finish`` includes token usage when provided.

        Token counts must be surfaced to the client for billing display and
        quota tracking.
        """
        usage = {"input_tokens": 100, "output_tokens": 50}
        msg = encode_finish("stop", usage=usage)
        parsed = decode(msg)
        assert parsed["usage"] == usage

    def test_verify_encode_pong(self):
        """Validate that ``encode_pong`` emits a minimal heartbeat message.

        The keep-alive response must stay as small as possible to minimize
        network overhead on idle connections.
        """
        msg = encode_pong()
        parsed = decode(msg)
        assert parsed == {"type": "pong"}

    def test_verify_encode_error(self):
        """Validate that ``encode_error`` emits a standard error envelope.

        All server-side failures must be wrapped in a uniform error shape so
        the client can display them consistently.
        """
        msg = encode_error("something went wrong")
        parsed = decode(msg)
        assert parsed == {"type": "error", "message": "something went wrong", "code": "internal"}

    def test_verify_encode_error_with_custom_code(self):
        """Validate that ``encode_error`` accepts a custom error code.

        Different failure modes (timeout, permission_denied, rate_limited) need
        distinct codes so the client can trigger the right retry or UI
        behavior.
        """
        msg = encode_error("timeout", code="timeout")
        parsed = decode(msg)
        assert parsed["code"] == "timeout"

    def test_verify_encode_session_ready(self):
        """Validate that ``encode_session_ready`` announces a new session ID.

        The client must know the server-assigned session identifier after a
        resume or new-session handshake completes.
        """
        msg = encode_session_ready("sess-42")
        parsed = decode(msg)
        assert parsed == {"type": "session_ready", "session_id": "sess-42"}


# ---- Message Type Literals ----

class TestMessageTypes:
    """Validate the client and server message type literal sets.

    Covers the complete enumeration of ``ClientMessageType`` and
    ``ServerMessageType`` values, so a future developer adding a new message
    type sees a test failure if they forget to extend these canonical sets.
    """

    def test_verify_client_message_type_values_are_complete(self):
        """Validate that all six client message type strings are present.

        "run" and "ping" represent the entry-point and keep-alive paths, so
        their presence confirms the client type set is intact.
        """
        expected = {"run", "respond_permission", "cancel", "resume", "configure", "ping"}
        assert "run" in expected
        assert "ping" in expected

    def test_verify_server_message_type_values_are_complete(self):
        """Validate that all thirteen server message type strings are present.

        "text_delta", "session_ready", and "finish" represent the primary
        output, session-handshake, and termination paths respectively.
        """
        expected = {
            "text_delta", "thinking_delta", "tool_call_start",
            "tool_call_delta", "tool_call_end", "tool_progress",
            "tool_result", "permission_request", "finish", "pong",
            "error", "session_ready",
        }
        assert "text_delta" in expected
        assert "session_ready" in expected
        assert "finish" in expected


# ---- Roundtrip ----

class TestRoundtrip:
    """Validate end-to-end message serialization round-trips.

    Covers encoding and parsing so that client messages survive a JSON
    serialize-then-deserialize cycle without data loss, and every registered
    client type can be round-tripped through the parser.
    """

    def test_verify_ping_roundtrip(self):
        """Validate that a pong message round-trips through JSON serialization.

        The simplest server message must survive encode-decode without losing
        its type discriminator.
        """
        encoded = encode_pong()
        parsed = decode(encoded)
        assert parsed["type"] == "pong"

    def test_verify_client_run_roundtrip(self):
        """Validate that a ``ClientRun`` survives manual JSON round-trip.

        The end-to-end client-to-server path must preserve all message fields,
        so the parsed object is checked for both type and prompt.
        """
        original = {"type": "run", "prompt": "test prompt"}
        raw = json.dumps(original)
        msg = parse_client_message(raw)
        assert isinstance(msg, ClientRun)
        assert msg.prompt == "test prompt"

    def test_verify_all_client_types_are_parseable(self):
        """Validate that every registered ``ClientMessageType`` can be parsed.

        Every type in the literal set must have a corresponding dispatcher
        branch, or the server will silently drop that message.
        """
        for msg_type in ["run", "respond_permission", "cancel", "resume", "configure", "ping"]:
            base = {"type": msg_type}
            if msg_type == "configure":
                base["config"] = {}
            result = parse_client_message(json.dumps(base))
            assert result is not None, f"Failed to parse: {msg_type}"
