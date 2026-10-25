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

"""Binary framing codec and handshake helpers for the voice WebSocket API.

Speech traffic on this endpoint is not JSON over WebSocket; every frame is
a small header followed by a length-prefixed payload. The header packs the
protocol version, header size, message type and flag bits into bitfields,
which means framing has to be done by hand with ``struct`` rather than with
a generic serializer.

``Message`` implements both directions of that codec (``marshal`` /
``unmarshal``) and decides which extra fields to emit from the message type
and flag combination. The ``async`` functions at the bottom of the module
are thin, intent-revealing wrappers that build the correct ``Message`` and
send it, so callers never have to remember the bitfield layout.
"""

import io
import logging
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, List

import websockets

logger = logging.getLogger(__name__)


class MsgType(IntEnum):
    """Message type enumeration.

    Each value occupies the high four bits of the second header byte, so
    the wire values are spaced to keep the low four bits free for flags.
    ``ServerACK`` is kept as an alias because several server messages are
    described as ACKs in the protocol documentation while using the
    audio-only framing.
    """

    Invalid = 0
    FullClientRequest = 0b1
    AudioOnlyClient = 0b10
    FullServerResponse = 0b1001
    AudioOnlyServer = 0b1011
    FrontEndResultServer = 0b1100
    Error = 0b1111

    # Alias
    ServerACK = AudioOnlyServer

    def __str__(self) -> str:
        """Render the symbolic name, falling back to the numeric value.

        Falls back to ``MsgType(n)`` because a bitfield value that is not a
        defined member would otherwise render as a blank name in logs.
        """
        return self.name if self.name else f"MsgType({self.value})"


class MsgTypeFlagBits(IntEnum):
    """Message type flag bits.

    These bits sit in the low four positions of the type byte and decide
    how the rest of the frame is laid out: whether a sequence number is
    present, and whether an event number (plus session identifiers)
    precedes the payload.
    """

    NoSeq = 0  # Non-terminal packet with no sequence
    PositiveSeq = 0b1  # Non-terminal packet with sequence > 0
    LastNoSeq = 0b10  # Last packet with no sequence
    NegativeSeq = 0b11  # Last packet with sequence < 0
    WithEvent = 0b100  # Payload contains event number (int32)


class VersionBits(IntEnum):
    """Version bits.

    The first four bits of the header byte select the protocol revision;
    only the numeric codes are meaningful on the wire.
    """

    Version1 = 1
    Version2 = 2
    Version3 = 3
    Version4 = 4


class HeaderSizeBits(IntEnum):
    """Header size bits.

    The value is a multiple of four bytes, so ``HeaderSize4`` means a
    three-byte header padded with one zero byte. Sizes larger than four
    leave room for optional header extensions.
    """

    HeaderSize4 = 1
    HeaderSize8 = 2
    HeaderSize12 = 3
    HeaderSize16 = 4


class SerializationBits(IntEnum):
    """Serialization method bits.

    Only JSON is used by this client; the remaining values exist so frames
    from other implementations can be inspected without parse errors.
    """

    Raw = 0
    JSON = 0b1
    Thrift = 0b11
    Custom = 0b1111


class CompressionBits(IntEnum):
    """Compression method bits.

    Named ``None_`` to avoid clashing with Python's ``None`` while keeping
    a readable symbol for the uncompressed default.
    """

    None_ = 0
    Gzip = 0b1
    Custom = 0b1111


class EventType(IntEnum):
    """Event type enumeration.

    Event numbers are grouped into ranges by direction (upstream/downstream)
    and by concern (connection, session, general, TTS, ASR, dialogue).
    Aliases such as ``StartTask`` equal ``StartConnection`` because the
    documentation describes some connection-level events under task
    vocabulary; exposing both names keeps call sites readable.
    """

    None_ = 0  # Default event

    # 1 ~ 49 Upstream Connection events
    StartConnection = 1
    StartTask = 1  # Alias of StartConnection
    FinishConnection = 2
    FinishTask = 2  # Alias of FinishConnection

    # 50 ~ 99 Downstream Connection events
    ConnectionStarted = 50  # Connection established successfully
    TaskStarted = 50  # Alias of ConnectionStarted
    ConnectionFailed = 51  # Connection failed (possibly due to authentication failure)
    TaskFailed = 51  # Alias of ConnectionFailed
    ConnectionFinished = 52  # Connection ended
    TaskFinished = 52  # Alias of ConnectionFinished

    # 100 ~ 149 Upstream Session events
    StartSession = 100
    CancelSession = 101
    FinishSession = 102

    # 150 ~ 199 Downstream Session events
    SessionStarted = 150
    SessionCanceled = 151
    SessionFinished = 152
    SessionFailed = 153
    UsageResponse = 154  # Usage response
    ChargeData = 154  # Alias of UsageResponse

    # 200 ~ 249 Upstream general events
    TaskRequest = 200
    UpdateConfig = 201

    # 250 ~ 299 Downstream general events
    AudioMuted = 250

    # 300 ~ 349 Upstream TTS events
    SayHello = 300

    # 350 ~ 399 Downstream TTS events
    TTSSentenceStart = 350
    TTSSentenceEnd = 351
    TTSResponse = 352
    TTSEnded = 359
    PodcastRoundStart = 360
    PodcastRoundResponse = 361
    PodcastRoundEnd = 362
    PodcastEnd = 363

    # 450 ~ 499 Downstream ASR events
    ASRInfo = 450
    ASRResponse = 451
    ASREnded = 459

    # 500 ~ 549 Upstream dialogue events
    ChatTTSText = 500  # (Ground-Truth-Alignment) text for speech synthesis

    # 550 ~ 599 Downstream dialogue events
    ChatResponse = 550
    ChatEnded = 559

    # 650 ~ 699 Downstream dialogue events
    # Events for source (original) language subtitle
    SourceSubtitleStart = 650
    SourceSubtitleResponse = 651
    SourceSubtitleEnd = 652
    # Events for target (translation) language subtitle
    TranslationSubtitleStart = 653
    TranslationSubtitleResponse = 654
    TranslationSubtitleEnd = 655

    def __str__(self) -> str:
        """Render the symbolic name, falling back to the numeric value."""
        return self.name if self.name else f"EventType({self.value})"


@dataclass
class Message:
    """A single framed protocol message.

    Message format:

    0                 1                 2                 3
    | 0 1 2 3 4 5 6 7 | 0 1 2 3 4 5 6 7 | 0 1 2 3 4 5 6 7 | 0 1 2 3 4 5 6 7 |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |    Version      |   Header Size   |     Msg Type    |      Flags      |
    |   (4 bits)      |    (4 bits)     |     (4 bits)    |     (4 bits)    |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    | Serialization   |   Compression   |           Reserved                |
    |   (4 bits)      |    (4 bits)     |           (8 bits)                |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                                                                       |
    |                   Optional Header Extensions                          |
    |                     (if Header Size > 1)                              |
    |                                                                       |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                                                                       |
    |                           Payload                                     |
    |                      (variable length)                                |
    |                                                                       |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

    Beyond the fixed header the frame optionally carries an event number,
    a session id, a sequence number, an error code and the payload itself;
    which subset is present is derived from ``type`` and ``flag`` in
    ``_get_writers`` / ``_get_readers``.
    """

    version: VersionBits = VersionBits.Version1
    header_size: HeaderSizeBits = HeaderSizeBits.HeaderSize4
    type: MsgType = MsgType.Invalid
    flag: MsgTypeFlagBits = MsgTypeFlagBits.NoSeq
    serialization: SerializationBits = SerializationBits.JSON
    compression: CompressionBits = CompressionBits.None_

    event: EventType = EventType.None_
    session_id: str = ""
    connect_id: str = ""
    sequence: int = 0
    error_code: int = 0

    payload: bytes = b""

    @classmethod
    def from_bytes(cls, data: bytes) -> "Message":
        """Decode a wire frame into a ``Message``.

        The type and flag bits are peeled off the second byte first because
        ``unmarshal`` needs to know the message type to decide which
        fields to read. A frame shorter than three bytes cannot even hold
        the fixed header, so it is rejected up front.
        """
        if len(data) < 3:
            raise ValueError(
                f"Data too short: expected at least 3 bytes, got {len(data)}"
            )

        type_and_flag = data[1]
        msg_type = MsgType(type_and_flag >> 4)
        flag = MsgTypeFlagBits(type_and_flag & 0b00001111)

        msg = cls(type=msg_type, flag=flag)
        msg.unmarshal(data)
        return msg

    def marshal(self) -> bytes:
        """Encode this message into its wire representation.

        The three fixed header bytes are packed as nibbles, then zero-padded
        up to ``4 * header_size`` so the optional extensions start on a
        4-byte boundary. Field writers are appended afterwards in the same
        order the decoder expects.
        """
        buffer = io.BytesIO()

        # Write header
        header = [
            (self.version << 4) | self.header_size,
            (self.type << 4) | self.flag,
            (self.serialization << 4) | self.compression,
        ]

        header_size = 4 * self.header_size
        if padding := header_size - len(header):
            header.extend([0] * padding)

        buffer.write(bytes(header))

        # Write other fields
        writers = self._get_writers()
        for writer in writers:
            writer(buffer)

        return buffer.getvalue()

    def unmarshal(self, data: bytes) -> None:
        """Decode ``data`` in place, populating every field of the frame.

        The header is consumed field-by-field; the padding bytes are
        skipped rather than parsed because their content is reserved. Any
        bytes left over after the payload are treated as a framing error,
        which surfaces protocol drift loudly instead of silently
        mis-aligning the next frame.
        """
        buffer = io.BytesIO(data)

        # Read version and header size
        version_and_header_size = buffer.read(1)[0]
        self.version = VersionBits(version_and_header_size >> 4)
        self.header_size = HeaderSizeBits(version_and_header_size & 0b00001111)

        # Skip second byte
        buffer.read(1)

        # Read serialization and compression methods
        serialization_compression = buffer.read(1)[0]
        self.serialization = SerializationBits(serialization_compression >> 4)
        self.compression = CompressionBits(serialization_compression & 0b00001111)

        # Skip header padding
        header_size = 4 * self.header_size
        read_size = 3
        if padding_size := header_size - read_size:
            buffer.read(padding_size)

        # Read other fields
        readers = self._get_readers()
        for reader in readers:
            reader(buffer)

        # Check for remaining data
        remaining = buffer.read()
        if remaining:
            raise ValueError(f"Unexpected data after message: {remaining}")

    def _get_writers(self) -> List[Callable[[io.BytesIO], None]]:
        """Build the ordered writer list for this frame.

        Field order on the wire is fixed: optional event/session id first,
        then the type-specific discriminator (sequence or error code), then
        the payload. An unsupported type raises immediately so a bad
        message cannot be half-serialised.
        """
        writers = []

        if self.flag == MsgTypeFlagBits.WithEvent:
            writers.extend([self._write_event, self._write_session_id])

        if self.type in [
            MsgType.FullClientRequest,
            MsgType.FullServerResponse,
            MsgType.FrontEndResultServer,
            MsgType.AudioOnlyClient,
            MsgType.AudioOnlyServer,
        ]:
            if self.flag in [MsgTypeFlagBits.PositiveSeq, MsgTypeFlagBits.NegativeSeq]:
                writers.append(self._write_sequence)
        elif self.type == MsgType.Error:
            writers.append(self._write_error_code)
        else:
            raise ValueError(f"Unsupported message type: {self.type}")

        writers.append(self._write_payload)
        return writers

    def _get_readers(self) -> List[Callable[[io.BytesIO], None]]:
        """Build the ordered reader list for this frame.

        The type-specific discriminator is read before the event block here,
        mirroring the wire order used for the corresponding fields, and the
        connect id is only ever read (never written) because the client
        receives it from the server.
        """
        readers = []

        if self.type in [
            MsgType.FullClientRequest,
            MsgType.FullServerResponse,
            MsgType.FrontEndResultServer,
            MsgType.AudioOnlyClient,
            MsgType.AudioOnlyServer,
        ]:
            if self.flag in [MsgTypeFlagBits.PositiveSeq, MsgTypeFlagBits.NegativeSeq]:
                readers.append(self._read_sequence)
        elif self.type == MsgType.Error:
            readers.append(self._read_error_code)
        else:
            raise ValueError(f"Unsupported message type: {self.type}")

        if self.flag == MsgTypeFlagBits.WithEvent:
            readers.extend(
                [self._read_event, self._read_session_id, self._read_connect_id]
            )

        readers.append(self._read_payload)
        return readers

    def _write_event(self, buffer: io.BytesIO) -> None:
        """Write event.

        Events are big-endian signed 32-bit so negative/custom event codes
        can be represented if a future revision needs them.
        """
        buffer.write(struct.pack(">i", self.event))

    def _write_session_id(self, buffer: io.BytesIO) -> None:
        """Write session ID.

        Connection-level events are scoped to the whole connection rather
        than to one session, so no session id is emitted for them — writing
        one would break the reader's field expectations.
        """
        if self.event in [
            EventType.StartConnection,
            EventType.FinishConnection,
            EventType.ConnectionStarted,
            EventType.ConnectionFailed,
        ]:
            return

        session_id_bytes = self.session_id.encode("utf-8")
        size = len(session_id_bytes)
        if size > 0xFFFFFFFF:
            raise ValueError(f"Session ID size ({size}) exceeds max(uint32)")

        buffer.write(struct.pack(">I", size))
        if size > 0:
            buffer.write(session_id_bytes)

    def _write_sequence(self, buffer: io.BytesIO) -> None:
        """Write sequence number.

        Signed because ``NegativeSeq`` frames use negative sequence values
        to mark the final chunk of a stream.
        """
        buffer.write(struct.pack(">i", self.sequence))

    def _write_error_code(self, buffer: io.BytesIO) -> None:
        """Write error code."""
        buffer.write(struct.pack(">I", self.error_code))

    def _write_payload(self, buffer: io.BytesIO) -> None:
        """Write payload.

        Length-prefixed with an unsigned 32-bit size so the reader can
        consume exactly the payload bytes and nothing else.
        """
        size = len(self.payload)
        if size > 0xFFFFFFFF:
            raise ValueError(f"Payload size ({size}) exceeds max(uint32)")

        buffer.write(struct.pack(">I", size))
        buffer.write(self.payload)

    def _read_event(self, buffer: io.BytesIO) -> None:
        """Read event.

        Reads defensively: a short read leaves ``event`` unchanged instead
        of raising, which keeps partially received frames usable.
        """
        event_bytes = buffer.read(4)
        if event_bytes:
            self.event = EventType(struct.unpack(">i", event_bytes)[0])

    def _read_session_id(self, buffer: io.BytesIO) -> None:
        """Read session ID.

        Mirrors ``_write_session_id`` for the connection-level events, and
        verifies the declared length matches the bytes actually available
        before decoding so a truncated frame cannot raise a UnicodeDecodeError.
        """
        if self.event in [
            EventType.StartConnection,
            EventType.FinishConnection,
            EventType.ConnectionStarted,
            EventType.ConnectionFailed,
            EventType.ConnectionFinished,
        ]:
            return

        size_bytes = buffer.read(4)
        if size_bytes:
            size = struct.unpack(">I", size_bytes)[0]
            if size > 0:
                session_id_bytes = buffer.read(size)
                if len(session_id_bytes) == size:
                    self.session_id = session_id_bytes.decode("utf-8")

    def _read_connect_id(self, buffer: io.BytesIO) -> None:
        """Read connection ID.

        Only the connection lifecycle events carry one, which is why the
        guard is event-specific rather than flag-specific.
        """
        if self.event in [
            EventType.ConnectionStarted,
            EventType.ConnectionFailed,
            EventType.ConnectionFinished,
        ]:
            size_bytes = buffer.read(4)
            if size_bytes:
                size = struct.unpack(">I", size_bytes)[0]
                if size > 0:
                    self.connect_id = buffer.read(size).decode("utf-8")

    def _read_sequence(self, buffer: io.BytesIO) -> None:
        """Read sequence number."""
        sequence_bytes = buffer.read(4)
        if sequence_bytes:
            self.sequence = struct.unpack(">i", sequence_bytes)[0]

    def _read_error_code(self, buffer: io.BytesIO) -> None:
        """Read error code."""
        error_code_bytes = buffer.read(4)
        if error_code_bytes:
            self.error_code = struct.unpack(">I", error_code_bytes)[0]

    def _read_payload(self, buffer: io.BytesIO) -> None:
        """Read payload.

        A zero declared length leaves ``payload`` empty rather than
        consuming bytes, so an empty frame is still well formed.
        """
        size_bytes = buffer.read(4)
        if size_bytes:
            size = struct.unpack(">I", size_bytes)[0]
            if size > 0:
                self.payload = buffer.read(size)

    def __str__(self) -> str:
        """String representation.

        Audio frames are logged by size rather than content because the
        payload is binary and dumping it would flood the log; error frames
        include the payload text since that is the actual diagnostic.
        """
        if self.type in [MsgType.AudioOnlyServer, MsgType.AudioOnlyClient]:
            if self.flag in [MsgTypeFlagBits.PositiveSeq, MsgTypeFlagBits.NegativeSeq]:
                return f"MsgType: {self.type}, EventType:{self.event}, Sequence: {self.sequence}, PayloadSize: {len(self.payload)}"
            return f"MsgType: {self.type}, EventType:{self.event}, PayloadSize: {len(self.payload)}"
        elif self.type == MsgType.Error:
            return f"MsgType: {self.type}, EventType:{self.event}, ErrorCode: {self.error_code}, Payload: {self.payload.decode('utf-8', 'ignore')}"
        else:
            if self.flag in [MsgTypeFlagBits.PositiveSeq, MsgTypeFlagBits.NegativeSeq]:
                return f"MsgType: {self.type}, EventType:{self.event}, Sequence: {self.sequence}, Payload: {self.payload.decode('utf-8', 'ignore')}"
            return f"MsgType: {self.type}, EventType:{self.event}, Payload: {self.payload.decode('utf-8', 'ignore')}"


async def receive_message(websocket: websockets.WebSocketClientProtocol) -> Message:
    """Receive message from websocket.

    Text frames are rejected outright because this protocol is strictly
    binary; accepting them would silently corrupt the byte stream. Any
    failure is logged with the underlying reason and re-raised so the
    caller decides whether to retry.
    """
    try:
        data = await websocket.recv()
        if isinstance(data, str):
            raise ValueError(f"Unexpected text message: {data}")
        elif isinstance(data, bytes):
            msg = Message.from_bytes(data)
            logger.info(f"Received: {msg}")
            return msg
        else:
            raise ValueError(f"Unexpected message type: {type(data)}")
    except Exception as e:
        logger.error(f"Failed to receive message: {e}")
        raise


async def wait_for_event(
    websocket: websockets.WebSocketClientProtocol,
    msg_type: MsgType,
    event_type: EventType,
) -> Message:
    """Wait for specific event.

    The handshake is strictly ordered, so any message that does not match
    the expected (type, event) pair is an error rather than something to
    buffer: treating it as unexpected makes protocol drift visible instead
    of letting a wrong frame be consumed by a later check.
    """
    while True:
        msg = await receive_message(websocket)
        if msg.type != msg_type or msg.event != event_type:
            raise ValueError(f"Unexpected message: {msg}")
        if msg.type == msg_type and msg.event == event_type:
            return msg


async def full_client_request(
    websocket: websockets.WebSocketClientProtocol, payload: bytes
) -> None:
    """Send full client message.

    Uses ``NoSeq`` because a standalone full request is both the first and
    last frame of its own stream.
    """
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.NoSeq)
    msg.payload = payload
    logger.info(f"Sending: {msg}")
    await websocket.send(msg.marshal())


async def audio_only_client(
    websocket: websockets.WebSocketClientProtocol, payload: bytes, flag: MsgTypeFlagBits
) -> None:
    """Send audio-only client message.

    The flag is passed in by the caller because streaming audio has to mark
    its final chunk with ``NegativeSeq`` so the server knows the stream is
    complete.
    """
    msg = Message(type=MsgType.AudioOnlyClient, flag=flag)
    msg.payload = payload
    logger.info(f"Sending: {msg}")
    await websocket.send(msg.marshal())


async def start_connection(websocket: websockets.WebSocketClientProtocol) -> None:
    """Start connection.

    Sends an empty JSON object as payload because the endpoint is selected
    by the URL and the auth details ride in HTTP headers.
    """
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.StartConnection
    msg.payload = b"{}"
    logger.info(f"Sending: {msg}")
    await websocket.send(msg.marshal())


async def finish_connection(websocket: websockets.WebSocketClientProtocol) -> None:
    """Finish connection.

    Tells the server no further sessions will be opened on this connection,
    allowing it to release per-connection state before the socket closes.
    """
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.FinishConnection
    msg.payload = b"{}"
    logger.info(f"Sending: {msg}")
    await websocket.send(msg.marshal())


async def start_session(
    websocket: websockets.WebSocketClientProtocol, payload: bytes, session_id: str
) -> None:
    """Start session.

    Carries the generation parameters as the payload and a fresh session id
    that the server echoes back in later events so responses can be
    correlated across concurrent sessions.
    """
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.StartSession
    msg.session_id = session_id
    msg.payload = payload
    logger.info(f"Sending: {msg}")
    await websocket.send(msg.marshal())


async def finish_session(
    websocket: websockets.WebSocketClientProtocol, session_id: str
) -> None:
    """Finish session.

    Closes the input side of an already-started session; the server may
    still push responses for work it accepted before this arrives.
    """
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.FinishSession
    msg.session_id = session_id
    msg.payload = b"{}"
    logger.info(f"Sending: {msg}")
    await websocket.send(msg.marshal())


async def cancel_session(
    websocket: websockets.WebSocketClientProtocol, session_id: str
) -> None:
    """Cancel session.

    Requests that in-flight work be aborted; unlike ``finish_session`` the
    server should not produce further output for this session.
    """
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.CancelSession
    msg.session_id = session_id
    msg.payload = b"{}"
    logger.info(f"Sending: {msg}")
    await websocket.send(msg.marshal())


async def task_request(
    websocket: websockets.WebSocketClientProtocol, payload: bytes, session_id: str
) -> None:
    """Send task request.

    Adds further work to an already-open session rather than opening a new
    one, which avoids paying the connection setup cost again.
    """
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.TaskRequest
    msg.session_id = session_id
    msg.payload = payload
    logger.info(f"Sending: {msg}")
    await websocket.send(msg.marshal())
