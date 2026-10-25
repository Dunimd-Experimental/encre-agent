/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 * 
 * DISCLAIMER: Users must comply with applicable AI regulations.
 * Non-compliance may result in service termination or legal liability.
 */

/**
 * WebSocket transport layer.
 *
 * Manages the renderer ↔ backend WebSocket connection (default port 7110):
 * connection lifecycle, auto-reconnect, a keep-alive ping, and AES-GCM
 * encryption of every frame in both directions.
 *
 * Transport encryption is mandatory: the backend closes any connection whose
 * first frame is not ciphertext, and this module never sends plaintext as a
 * fallback. If the master key is unavailable the socket is simply not opened.
 * Outbound messages are queued until the socket is open.
 */

import { ClientMessage, ServerEvent } from "./types.js";
import { initCrypto, encrypt, decrypt, isReady } from "./crypto.js";
import { reportUiFault } from "./ui_boundary.js";
import { setConnected, getState, showToast } from "./state.js";

type EventHandler = (event: ServerEvent) => void;

const WS_PORT = 7110;

/** How long the initial connect() keeps retrying while the backend boots. */
const CONNECT_TIMEOUT_MS = 45_000;

/** True while the initial connect() call is retrying — suppresses the
 *  background scheduleReconnect() path so it cannot double-connect. */
let _initialConnect = false;

/** Set by abortConnect() to cut the initial connect retry loop short (e.g.
 *  when the backend process is known to have died — retrying a dead port is
 *  just wasted time before the error UI can appear). */
let _connectAborted = false;

/** Cancels the current initial connect() retry loop early. */
export function abortConnect(): void {
  _connectAborted = true;
}

let ws: WebSocket | null = null;
let handler: EventHandler | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let pingTimer: ReturnType<typeof setInterval> | null = null;
let pendingQueue: ClientMessage[] = [];

/**
 * Tracks the last workspace open/close the renderer asked for. The backend
 * answers `open_workspace` asynchronously (session setup + index start can
 * take seconds) and `workspace_opened` carries no request id, so a response
 * that lands *after* the user already left would otherwise drag the UI back
 * into iWork. stream.ts checks this before applying a `workspace_opened`.
 */
let _workspaceIntent: "open" | "close" | "" = "";

/** The last workspace intent sent by the renderer (see `send`). */
export function getWorkspaceIntent(): "open" | "close" | "" {
  return _workspaceIntent;
}

async function drainQueue(): Promise<void> {
  const q = pendingQueue;
  pendingQueue = [];
  for (const msg of q) {
    await send(msg);
  }
}

/**
 * Opens the WebSocket connection and wires up event/lifecycle handlers.
 * Retries automatically (up to CONNECT_TIMEOUT_MS) so a window that
 * appears before the Python backend finishes booting never surfaces a
 * spurious connection error.
 *
 * @param onEvent - Handler invoked for every (decrypted) server event.
 */
export async function connect(onEvent: EventHandler): Promise<void> {
  handler = onEvent;

  const url = `ws://localhost:${WS_PORT}/ws`;
  _initialConnect = true;
  _connectAborted = false;
  try {
    const deadline = Date.now() + CONNECT_TIMEOUT_MS;
    for (;;) {
      // The backend rejects any connection whose first frame is plaintext, so
      // opening a socket before the master key is unwrapped is pointless. The
      // keyfile is written by the backend at startup, so retry the unwrap
      // alongside the connect until either succeeds or the deadline passes.
      if (!isReady()) {
        await initCrypto();
      }
      if (isReady()) {
        const opened = await openSocket(url);
        if (opened) break;
      }
      if (_connectAborted) break;
      if (Date.now() >= deadline) break;
      await new Promise<void>((r) => setTimeout(r, 400));
    }
  } catch (e) {
    console.warn("[ws] connect failed:", e);
  } finally {
    _initialConnect = false;
    // If we gave up, keep the usual reconnect machinery retrying in the
    // background (matches the previous behaviour after a failed connect).
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      scheduleReconnect();
    }
  }
}

/**
 * Creates a single WebSocket attempt and wires up the event handlers.
 * Resolves true once the socket is open, false if the attempt failed.
 */
function openSocket(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const socket = new WebSocket(url);
    ws = socket;

    socket.addEventListener("open", () => {
      if (settled) return;
      settled = true;
      handler?.({ type: "pong" });
      setConnected(true);
      startPing();
      drainQueue();
      // The backend's handshake peek waits briefly for a first frame and then
      // closes the socket if none arrived, so open with an encrypted frame
      // rather than staying silent until the 25s keep-alive. The backend's
      // pong reply doubles as a round-trip probe.
      void send({ type: "ping" });
      resolve(true);
    });

    // Frames arrive in order, but `await decrypt()` resolves on the microtask
    // queue: independent async listeners could finish out of order and apply
    // events backwards (text rewinds, stale tool status wins). Chain them so
    // decryption stays async while application stays strictly ordered.
    let inboundChain: Promise<void> = Promise.resolve();
    let sliceStart = performance.now();
    socket.addEventListener("message", (evt: MessageEvent<string>) => {
      const raw = evt.data;
      inboundChain = inboundChain
        .then(async () => {
          if (!raw || typeof raw !== "string") {
            throw new Error("[ws] received non-text frame");
          }
          // Every server frame is ciphertext. A decrypt/parse failure means
          // the peer violated the protocol — surface it loudly.
          const _decodeStarted = performance.now();
          const decrypted = await decrypt(raw);
          const event = JSON.parse(decrypted) as ServerEvent;
          const _decodeMs = performance.now() - _decodeStarted;
          if (_decodeMs >= 30) {
            console.warn(`[perf] ws decode ${event.type} ${Math.round(_decodeMs)}ms`);
          }
          try {
            handler?.(event);
          } catch (handlerErr) {
            // One bad event must not tear down a healthy transport or the
            // inbound chain; it is recorded by the shared UI boundary.
            reportUiFault("event", (event as { type?: string }).type || "event", handlerErr);
          }
          // The connect snapshot arrives as a burst of frames. Promise-chain
          // continuations are microtasks, so without an explicit yield the
          // whole burst (and any DOM work it triggers) runs before the
          // browser handles a click or paints. Yielding every ~8 ms keeps the
          // title bar and sidebar responsive while boot data streams in.
          if (performance.now() - sliceStart >= 8) {
            await new Promise<void>((resolve) => setTimeout(resolve, 0));
            sliceStart = performance.now();
          }
        })
        .catch((e) => {
          reportUiFault("transport", "decode", e);
          // Fail closed: tear the socket down rather than keep a connection
          // whose framing we no longer understand. The normal reconnect path
          // will re-establish it once both sides agree again.
          socket.close();
        });
    });

    socket.addEventListener("close", () => {
      stopPing();
      setConnected(false);
      if (!_initialConnect) {
        scheduleReconnect();
      }
      if (!settled) {
        settled = true;
        resolve(false);
      }
    });

    socket.addEventListener("error", () => {
      socket.close();
    });
  });
}

/**
 * Sends a client message, encrypted with AES-GCM. There is no plaintext path:
 * an open socket implies the master key was unwrapped before connecting.
 *
 * @param msg - The client message to transmit. Queued if the socket is not open.
 */
let sendChain: Promise<void> = Promise.resolve();

async function deliver(msg: ClientMessage): Promise<void> {
  if (ws?.readyState !== WebSocket.OPEN) {
    pendingQueue.push(msg);
    return;
  }
  const encrypted = await encrypt(JSON.stringify(msg));
  ws.send(encrypted);
}

export function send(msg: ClientMessage): Promise<void> {
  // Record workspace intent before the (possibly queued) send so a late
  // response can be recognised as stale even if the socket was reconnecting.
  if (msg.type === "open_workspace") _workspaceIntent = "open";
  else if (msg.type === "close_workspace") _workspaceIntent = "close";

  // Encryption is async, so two callers racing would otherwise reach
  // `ws.send` in completion order rather than call order (e.g. a cancel
  // overtaking the run it was meant to stop). Chain them.
  sendChain = sendChain
    .then(() => deliver(msg))
    .catch((err) => {
      console.warn("[ws] send failed:", err);
      showToast("Send failed", "", "error", "WebSocket");
    });
  return sendChain;
}

/** Sends a `retry` request for a given branch/message index. */
export function sendRetry(branch_id: string, user_message_index: number, mode?: "normal" | "detailed" | "concise"): void {
  send({ type: "retry", branch_id, user_message_index, mode, session_id: getState().sessionId });
}

/** Sends a `switch_branch` request to change the active branch. */
export function sendSwitchBranch(branch_id: string): void {
  send({ type: "switch_branch", branch_id, session_id: getState().sessionId });
}

/** Sends a `rollback` request to revert to an earlier message in a branch.
 *  Always carries session_id -- see ClientRollback, the backend binds an
 *  omitted one to the connection's last session. */
export function sendRollback(branch_id: string, message_id: string, session_id?: string): void {
  send({ type: "rollback", branch_id, message_id, session_id: session_id || getState().sessionId });
}

/** Sends the CDP WebSocket URL for the internal browser webview to the backend. */
export function sendSetCdpUrl(url: string): void {
  send({ type: "browser_cdp_url", url, session_id: getState().sessionId });
}

function startPing(): void {
  stopPing();
  pingTimer = setInterval(() => {
    send({ type: "ping" });
  }, 25000);
}

function stopPing(): void {
  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
}

function scheduleReconnect(): void {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect(handler!);
  }, 2000);
}

/** Closes the connection, stops ping and cancels any pending reconnect. */
export function disconnect(): void {
  stopPing();
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  ws?.close();
  ws = null;
}
