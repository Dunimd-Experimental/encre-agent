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
 * connection lifecycle, auto-reconnect, a keep-alive ping, transparent
 * AES-GCM encryption of outbound messages, and decryption of inbound events.
 * Outbound messages are queued until the socket is open.
 */

import { ClientMessage, ServerEvent } from "./types.js";
import { initCrypto, encrypt, decrypt, isReady } from "./crypto.js";
import { setConnected, getState, showToast } from "./state.js";

type EventHandler = (event: ServerEvent) => void;

const WS_PORT = 7110;

/** How long the initial connect() keeps retrying while the backend boots. */
const CONNECT_TIMEOUT_MS = 45_000;

/** True while the initial connect() call is retrying — suppresses the
 *  background scheduleReconnect() path so it cannot double-connect. */
let _initialConnect = false;

let ws: WebSocket | null = null;
let handler: EventHandler | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let pingTimer: ReturnType<typeof setInterval> | null = null;
let _cryptoInitDone = false;
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

  // Initialise the crypto layer before the first connection. The keyfile is
  // written by the backend, which may not have finished booting yet, so a
  // failed attempt is retried on later connects instead of latching the
  // session into permanent plaintext.
  if (!_cryptoInitDone || !isReady()) {
    await initCrypto();
    _cryptoInitDone = true;
  }

  const url = `ws://localhost:${WS_PORT}/ws`;
  _initialConnect = true;
  try {
    const deadline = Date.now() + CONNECT_TIMEOUT_MS;
    for (;;) {
      const opened = await openSocket(url);
      if (opened) break;
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
      resolve(true);
    });

    socket.addEventListener("message", async (evt: MessageEvent<string>) => {
      try {
        let raw = evt.data;
        let event: ServerEvent;

        if (isReady() && raw && typeof raw === "string" && !raw.startsWith("{")) {
          // Encrypted ciphertext — decrypt then parse
          try {
            const decrypted = await decrypt(raw);
            event = JSON.parse(decrypted) as ServerEvent;
          } catch {
            // Fallback: try plaintext parse
            event = JSON.parse(raw) as ServerEvent;
          }
        } else {
          // Plaintext (legacy or crypto unavailable)
          event = JSON.parse(raw) as ServerEvent;
        }

        handler?.(event);
      } catch (e) {
        console.warn("[ws] malformed message:", e);
      }
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
 * Sends a client message, encrypting it when the crypto layer is ready.
 *
 * @param msg - The client message to transmit. Queued if the socket is not open.
 */
export async function send(msg: ClientMessage): Promise<void> {
  // Record workspace intent before the (possibly queued) send so a late
  // response can be recognised as stale even if the socket was reconnecting.
  if (msg.type === "open_workspace") _workspaceIntent = "open";
  else if (msg.type === "close_workspace") _workspaceIntent = "close";

  if (ws?.readyState !== WebSocket.OPEN) {
    pendingQueue.push(msg);
    return;
  }
  try {
    const payload = JSON.stringify(msg);
    if (isReady()) {
      const encrypted = await encrypt(payload);
      ws.send(encrypted);
    } else {
      ws.send(payload);
    }
  } catch (err) {
    console.warn("[ws] send failed:", err);
    showToast("Send failed", "", "error", "WebSocket");
  }
}

/** Sends a `retry` request for a given branch/message index. */
export function sendRetry(branch_id: string, user_message_index: number, mode?: "normal" | "detailed" | "concise"): void {
  send({ type: "retry", branch_id, user_message_index, mode, session_id: getState().sessionId });
}

/** Sends a `switch_branch` request to change the active branch. */
export function sendSwitchBranch(branch_id: string): void {
  send({ type: "switch_branch", branch_id, session_id: getState().sessionId });
}

/** Sends a `rollback` request to revert to an earlier message in a branch. */
export function sendRollback(branch_id: string, message_id: string): void {
  send({ type: "rollback", branch_id, message_id });
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
