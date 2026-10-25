/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
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
 * The submit queue as a pending-panel source.
 *
 * The queue itself lives server-side (persisted across restarts and mirrored
 * into `state.queuedPrompts` by the `queued_submits` broadcast), so nothing
 * here mutates the list locally: every row action goes out as a `queue_*`
 * message and the refreshed snapshot that comes back repaints the card.
 */

import { getState } from "../core/state.js";
import { send } from "../core/ws.js";
import { t } from "../features/i18n.js";
import { registerPendingSource, type PendingRow, type PendingRowAction } from "../features/pending-panel.js";

/** The composer, as seen from the queue: the host puts a pulled-back prompt
 *  back on screen and takes the caret with it. */
export interface QueueEditHost {
  loadForEdit(text: string, mode?: string): void;
}

/**
 * Send one queued prompt now: take it to the head, and — if a turn is running —
 * cancel that turn so the backend's drain picks this entry up.
 */
function sendDirectly(index: number): void {
  const st = getState();
  if (index > 0) {
    send({ type: "queue_move", session_id: st.sessionId, from_index: index, to_index: 0 });
  }
  if (st.running) {
    const btnStop = document.getElementById("btn-stop");
    if (btnStop) {
      btnStop.classList.add("cancelling");
      btnStop.style.pointerEvents = "none";
    }
    send({ type: "cancel", session_id: st.sessionId });
  } else {
    send({ type: "queue_pop", session_id: st.sessionId });
  }
}

export function registerQueueSource(host: QueueEditHost): void {
  registerPendingSource({
    id: "queue",
    icon: "clock",
    // Empty string = rows carry no leading icon (the text alone is the row).
    rowIcon: "",
    name: () => t("chat.queueTitle"),
    status: () => t("chat.waitingToSend"),
    watch: ["queuedPrompts", "running"],
    rows: (): PendingRow[] => {
      const st = getState();
      const sessionId = st.sessionId;
      return st.queuedPrompts.map((p, i) => {
        const actions: PendingRowAction[] = [
          // Take the prompt back out of the queue and into the composer.  The
          // removal is server-side, so the entry cannot linger in a queue the
          // user already thinks they are editing.
          {
            icon: "pen-line",
            tooltip: t("chat.edit"),
            run: () => {
              send({ type: "queue_remove", session_id: sessionId, index: i });
              host.loadForEdit(p.text, p.mode);
            },
          },
        ];
        // Mid-turn steering only exists while something is actually running.
        if (st.running) {
          actions.push({
            icon: "corner-down-right",
            tooltip: t("slashCommands.steerTitle"),
            run: () => send({ type: "queue_steer", session_id: sessionId, index: i }),
          });
        }
        actions.push(
          {
            icon: "arrow-up",
            tooltip: t("chat.sendDirectly"),
            run: () => sendDirectly(i),
          },
          {
            icon: "trash-2",
            tooltip: t("common.delete"),
            run: () => send({ type: "queue_remove", session_id: sessionId, index: i }),
          },
        );
        // `mode` is never displayed, but a prompt re-queued under another mode
        // must still repaint, so it contributes to the row's cache key.
        return { text: p.text, key: p.mode || "", actions };
      });
    },
    reorder: (from, to) =>
      send({ type: "queue_move", session_id: getState().sessionId, from_index: from, to_index: to }),
  });
}
