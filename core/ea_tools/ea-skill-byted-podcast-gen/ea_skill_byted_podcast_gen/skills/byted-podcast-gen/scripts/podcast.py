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

"""Command-line client for ByteDance's multi-speaker podcast TTS service.

The service is streamed over a single WebSocket connection with a
three-phase handshake (connection → session → streaming) described by the
``protocols`` package. This script drives that handshake, accumulates the
per-round audio chunks the server pushes, and emits either the fully
concatenated podcast file, the generated dialogue text, or a hosted audio
URL depending on the requested action.

Three input modes are supported via ``--action``: raw text (0), a JSON
list of dialogue lines (3), and an open-ended prompt the service expands
into a dialogue (4). Output is a single JSON object on stdout so the
calling skill layer can consume it without parsing logs.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid

import websockets
from api_key import get_speech_api_key
from protocols import finish_connection  # noqa: E402
from protocols import (
    EventType,
    MsgType,
    finish_session,
    receive_message,
    start_connection,
    start_session,
    wait_for_event,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PodcastTTS")

# Resource ID pins the request to the podcast-TTS service variant on the
# gateway; it must be sent as a header rather than in the JSON body.
ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sami/podcasttts"


def _load_json_value(value: str):
    """Decode a JSON value that may be either inline JSON or a file path.

    Letting callers pass either form keeps the CLI ergonomic: a short
    dialogue can be inlined, while a long one can be read from disk. The
    existence check takes precedence so that a literal string such as
    ``"output.json"`` that happens to exist on disk is treated as a path
    rather than as a JSON error.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if os.path.exists(value):
        with open(value, "r", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(value)


def _output_dir_from_output_path(output_path: str) -> str:
    """Derive the directory that intermediate files should be written to.

    When an explicit output path is given, its parent directory is reused
    so all artifacts of one run land together; otherwise a default
    ``output/`` folder keeps generated media out of the caller's cwd.
    """
    if output_path:
        d = os.path.dirname(output_path)
        return d if d else "output"
    return "output"


def _final_extension(encoding: str) -> str:
    """Map a requested encoding name onto the on-disk file extension.

    ``ogg_opus`` is the protocol's option name but the resulting container
    is an OGG file, so the extension is ``ogg`` while every other encoding
    name matches its extension verbatim.
    """
    return "ogg" if encoding == "ogg_opus" else encoding


async def _generate(args) -> dict:
    """Run the full podcast generation flow over one WebSocket session.

    The sequence mirrors the server's protocol: authenticate the
    connection, open a session carrying the generation parameters, then
    close the input side so the server knows no more requests are coming.
    After that the loop consumes messages until ``SessionFinished`` and
    classifies them by (type, event) pair:

    * ``PodcastRoundStart`` carries the dialogue line being spoken; it is
      recorded so the text-only output mode has data even though audio
      may be discarded.
    * ``PodcastRoundResponse`` audio frames are appended to the current
      round buffer; they are only merged into the master buffer once the
      round is confirmed good at ``PodcastRoundEnd``, which means a failed
      round never corrupts the final concatenation.
    * ``PodcastEnd`` reports metadata such as the hosted ``audio_url``.

    Errors from the server are raised so ``main`` can surface them as a
    JSON error object instead of a traceback.
    """
    api_key = get_speech_api_key()
    if not api_key:
        raise ValueError("Missing MODEL_SPEECH_API_KEY")
    resource_id = "volc.service_type.10050"
    endpoint = ENDPOINT
    return_audio_url = True
    skip_round_audio_save = True

    if args.action == 0 and not (args.text or args.input_url):
        raise ValueError("action=0 requires --text or --input_url")
    if args.action == 3 and not args.nlp_texts:
        raise ValueError("action=3 requires --nlp_texts")
    if args.action == 4 and not args.prompt_text:
        raise ValueError("action=4 requires --prompt_text")

    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    output_dir = _output_dir_from_output_path(args.output)
    os.makedirs(output_dir, exist_ok=True)

    session_id = str(uuid.uuid4())

    speaker_info = _load_json_value(args.speaker_info) if args.speaker_info else None
    nlp_texts = _load_json_value(args.nlp_texts) if args.nlp_texts else None

    req_params = {
        "input_id": args.input_id,
        "input_text": args.text,
        "nlp_texts": nlp_texts,
        "prompt_text": args.prompt_text,
        "action": args.action,
        "use_head_music": args.use_head_music,
        "use_tail_music": args.use_tail_music,
        "aigc_watermark": args.aigc_watermark,
        "input_info": {
            "input_url": args.input_url,
            "return_audio_url": return_audio_url,
            "only_nlp_text": args.only_nlp_text,
        },
        "speaker_info": speaker_info,
        "audio_config": {
            "format": args.encoding,
            "sample_rate": 24000,
            "speech_rate": 0,
        },
    }

    podcast_audio = bytearray()
    round_audio = bytearray()
    podcast_texts = []
    audio_received = False
    current_round = 0
    current_speaker = "speaker"
    podcast_end_payload = None

    async with websockets.connect(endpoint, additional_headers=headers) as websocket:
        await start_connection(websocket)
        await wait_for_event(
            websocket, MsgType.FullServerResponse, EventType.ConnectionStarted
        )

        await start_session(
            websocket,
            json.dumps(req_params, ensure_ascii=False).encode("utf-8"),
            session_id,
        )
        await wait_for_event(
            websocket, MsgType.FullServerResponse, EventType.SessionStarted
        )
        await finish_session(websocket, session_id)

        while True:
            msg = await receive_message(websocket)

            if (
                msg.type == MsgType.AudioOnlyServer
                and msg.event == EventType.PodcastRoundResponse
            ):
                if msg.payload:
                    audio_received = True
                    round_audio.extend(msg.payload)
                continue

            if msg.type == MsgType.Error:
                raise RuntimeError(msg.payload.decode("utf-8", errors="replace"))

            if msg.type == MsgType.FullServerResponse:
                if msg.event == EventType.PodcastRoundStart:
                    data = json.loads(msg.payload.decode("utf-8", errors="replace"))
                    if data.get("text"):
                        podcast_texts.append(
                            {"text": data.get("text"), "speaker": data.get("speaker")}
                        )
                    current_speaker = data.get("speaker") or "speaker"
                    current_round = (
                        data.get("round_id") if data.get("round_id") is not None else 0
                    )
                    continue

                if msg.event == EventType.PodcastRoundEnd:
                    data = json.loads(msg.payload.decode("utf-8", errors="replace"))
                    if data.get("is_error"):
                        raise RuntimeError(json.dumps(data, ensure_ascii=False))
                    if round_audio:
                        if not skip_round_audio_save:
                            round_path = os.path.join(
                                output_dir,
                                f"{current_speaker}_{current_round}.{_final_extension(args.encoding)}",
                            )
                            with open(round_path, "wb") as f:
                                f.write(round_audio)
                        podcast_audio.extend(round_audio)
                        round_audio.clear()
                    continue

                if msg.event == EventType.PodcastEnd:
                    podcast_end_payload = json.loads(
                        msg.payload.decode("utf-8", errors="replace")
                    )
                    continue

            if msg.event == EventType.SessionFinished:
                break

        await finish_connection(websocket)
        await wait_for_event(
            websocket, MsgType.FullServerResponse, EventType.ConnectionFinished
        )

    result = {"status": "success", "task_id": session_id, "encoding": args.encoding}

    if podcast_texts:
        result["texts"] = json.dumps(podcast_texts, ensure_ascii=False, indent=2)

    if args.only_nlp_text:
        return result

    if not audio_received:
        raise RuntimeError("No audio data received")

    if podcast_audio:
        audio_path = args.output or os.path.join(
            output_dir, f"podcast_{session_id}.{_final_extension(args.encoding)}"
        )
        with open(audio_path, "wb") as f:
            f.write(podcast_audio)
        result["audio_path"] = audio_path

    if return_audio_url and isinstance(podcast_end_payload, dict):
        audio_url = podcast_end_payload.get("meta_info", {}).get("audio_url")
        if audio_url:
            result["audio_url"] = audio_url

    return result


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser describing the three generation modes.

    ``choices`` on ``--action`` and ``--encoding`` keeps invalid input from
    ever reaching the API; the mode-specific required inputs are validated
    later in ``_generate`` so the error message can name the offending
    action instead of a bare missing option.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action", default=4, type=int, choices=[0, 3, 4], help="Podcast type"
    )
    parser.add_argument("--text", default="", help="Input text (action=0)")
    parser.add_argument("--input_url", default="", help="Input text URL (action=0)")
    parser.add_argument(
        "--nlp_texts",
        default="",
        help="NLP texts JSON string or JSON file path (action=3)",
    )
    parser.add_argument("--prompt_text", default="", help="Prompt text (action=4)")

    parser.add_argument(
        "--encoding",
        default="mp3",
        choices=["mp3", "wav", "ogg_opus"],
        help="Audio format",
    )
    parser.add_argument(
        "--input_id", default="podcast_input", help="Unique input identifier"
    )
    parser.add_argument(
        "--speaker_info", default='{"random_order":false}', help="Speaker info JSON"
    )

    parser.add_argument(
        "--use_head_music", action="store_true", help="Enable head music"
    )
    parser.add_argument(
        "--use_tail_music", action="store_true", help="Enable tail music"
    )
    parser.add_argument(
        "--aigc_watermark", action="store_true", help="Enable aigc watermark"
    )
    parser.add_argument(
        "--only_nlp_text", action="store_true", help="Only output podcast texts"
    )

    parser.add_argument("--output", default="", help="Final audio output path")
    parser.add_argument("--texts_output", default="", help="Podcast texts output path")
    return parser


def main() -> int:
    """CLI entry point: run generation and emit a machine-readable result.

    Success and failure are both reported as JSON on stdout (with an exit
    code of 0 or 1) rather than as traceback text, so an embedding skill
    can always parse the last line reliably.
    """
    args = _build_parser().parse_args()
    try:
        result = asyncio.run(_generate(args))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
