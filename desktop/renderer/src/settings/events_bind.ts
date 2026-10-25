// @ts-nocheck
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

import { getState, setSettings, setCustomCommands, setTheme, setThemePreference, setPermissionPolicies, subscribe, showToast, isEnabled } from "../core/state.js";
import { send } from "../core/ws.js";
import { waitForModelValidation, onWechatScanResult, refreshAllData } from "../core/stream.js";
import { setModelConfigs, setMcpServers, setProviders, setSkillsList, setSubAgents } from "../core/state.js";
import type { ModelConfigMeta, MCPServerConfig, SkillInfo, ModelCatalog, McpCatalog, McpProviderEntry, ProviderEntry, ProfileData, CustomCommand, UsageStatsSessionEntry } from "../core/types.js";
import { defaultSearchFilter } from "../core/types.js";
import { scheduleRender } from "../core/render_scheduler.js";
import { Dialog } from "../ui/dialog.js";
import {
  bindTargetModelSelection,
  parseTargetModelSelection,
  readTargetModelSelection,
  renderTargetModelSelection,
  serializeTargetModelSelection,
} from "./model-selection.js";
import { t, initLocale, setLocale, getLocale, getIntlLocale, clearLocaleCache, onLocaleChange, LOCALES, type Locale } from "../features/i18n.js";
import { applyServerCommands } from "../ui/slash-commands.js";
import { renderMarkdown } from "../chat/chat.js";
import { platformIconHtml, searchEngineIconSrc } from "../ui/icons.js";
import { formatShortcut } from "../ui/shortcut-display.js";
// Chart.js is lazy-loaded on first render of the usage panel (see
// _renderUsageSection) so it no longer blocks startup parsing.
import { showTooltipAt, hideTooltip } from "../ui/tooltip.js";
import { SEARCH_ENGINES, getDefaultSearchEngine } from "../features/browser.js";
import { searchSettingsNavItems, SEARCH_FILTER_META, SETTINGS_NAV_ITEMS } from "../features/search.js";
import { setSearchFilter } from "../core/state.js";

/**
 * Wires every panel DOM listener / state subscription owned by the Settings
 * constructor. Kept as a module-level function so settings.ts can stay under
 * the 800-line budget while `this` still resolves to the Settings instance.
 */
export function initSettingsDomEvents(this: any): void {
    this.nav.addEventListener("click", (e) => {
      const target = (e.target as HTMLElement).closest(".settings-nav-item") as HTMLElement | null;
      if (!target) return;
      const panel = target.getAttribute("data-panel") as PanelId;
      if (panel) this.switchPanel(panel);
    });

    // Close any open dropdowns when clicking outside
    document.addEventListener("click", (e) => {
      const target = e.target as Node;
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => {
        const wrap = dd.closest(".settings-dropdown-wrap");
        if (wrap && !wrap.contains(target)) {
          dd.classList.remove("open");
        }
      });
    });

    // ── Event delegation on panel containers ─────────────────────────
    // These survive innerHTML replacement — no need to re-bind after render.

    // Library panel: auto-refresh on backend tree/content pushes. All click
    // logic is bound inside the panel itself (delegated on the library
    // containers in _bindLibraryInteractions), so nothing to route here.
    let lastLibTreeRef = getState().libraryTree;
    let lastLibContentRef = getState().libraryContent;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const st = getState();
      if (st.libraryTree !== lastLibTreeRef) {
        lastLibTreeRef = st.libraryTree;
        if (this.panels.library) this.refreshLibraryTree();
      }
      if (st.libraryContent !== lastLibContentRef) {
        lastLibContentRef = st.libraryContent;
        if (this.panels.library) this.refreshLibraryContent();
      }
    }, ["libraryTree", "libraryContent"]);

    // Skills panel: skill view/edit/delete, command add/edit/delete, rules
    // (the rules table now renders inline in this panel, so its own bindings
    //  live in renderRules() — nothing to route here).
    this.panels.skills.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;

      // View skill
      const viewBtn = target.closest("[data-action='view-skill']");
      if (viewBtn) {
        const name = viewBtn.getAttribute("data-name") || "";
        if (name) this.showSkillDetail(name);
        return;
      }

      // Edit skill
      const editBtn = target.closest("[data-action='edit-skill']");
      if (editBtn) {
        const name = editBtn.getAttribute("data-name") || "";
        if (name) this.showSkillDetail(name, true);
        return;
      }

      // Delete skill
      const deleteBtn = target.closest("[data-action='delete-skill']");
      if (deleteBtn) {
        const name = deleteBtn.getAttribute("data-name") || "";
        if (name) {
          Dialog.confirm(t("settings.delete"), t("settings.skillDeleteConfirm", { name }), { scope: "panel" }).then(async (ok) => {
            if (ok) {
              const filtered = getState().skillsList.filter(s => s.name !== name);
              setSkillsList(filtered);
              send({ type: "uninstall_skill", name });
              this.renderSkills();
            }
          });
        }
        return;
      }

      // Edit command
      const editCmd = target.closest("[data-action='edit-command']");
      if (editCmd) {
        const name = editCmd.getAttribute("data-name") || "";
        if (name) this.showCommandCreate(name);
        return;
      }

      // Delete command
      const deleteCmd = target.closest("[data-action='delete-command']");
      if (deleteCmd) {
        const name = deleteCmd.getAttribute("data-name") || "";
        if (name) {
          Dialog.confirm(t("settings.delete"), t("settings.commandRemoveConfirm", { name }), { scope: "panel" }).then(async (ok) => {
            if (ok) {
              this.removeCustomCommand(name);
            }
          });
        }
        return;
      }

      // Install skill button
      const installBtn = target.closest("#btn-install-skill");
      if (installBtn) {
        this.installSkill();
        return;
      }

      // Add command button
      const addCmd = target.closest("#btn-add-command");
      if (addCmd) {
        this.showCommandCreate();
        return;
      }
    });

    // Skills panel: toggle switches (change event)
    this.panels.skills.addEventListener("change", (e) => {
      const cb = (e.target as HTMLElement).closest(".skill-toggle") as HTMLInputElement | null;
      if (cb) {
        const checked = new Set<string>();
        this.panels.skills.querySelectorAll(".skill-toggle").forEach((el) => {
          if ((el as HTMLInputElement).checked) {
            checked.add((el as HTMLInputElement).getAttribute("data-skill") || "");
          }
        });
        send({ type: "update_skills", enabled_skills: Array.from(checked) });
      }

    });

    // ── Provider panel: edit, delete ────────────────────────────────
    // Models are enabled/disabled and edited inside the provider dialog;
    // the list itself only needs edit / delete / add entry points.
    this.panels.model.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;

      const poolSettingsBtn = target.closest("#btn-model-pool-settings");
      if (poolSettingsBtn) {
        this.showModelPoolSettings(0);
        return;
      }

      const deleteBtn = target.closest("[data-action='delete']");
      if (deleteBtn) {
        const idx = parseInt(deleteBtn.getAttribute("data-idx") || "0");
        const prov = (getState().providers || [])[idx];
        const name = (prov ? this._providerDisplayName?.(prov) : "") || prov?.backend_type || t("common.unnamed");
        Dialog.confirm(t("settings.confirmRemoveProviderTitle"), t("settings.confirmRemoveProvider", { name }), { scope: "panel" }).then((ok) => {
          if (!ok) return;
          const current = [...(getState().providers || [])];
          if (idx < 0 || idx >= current.length) return;
          // Shift the flat active-model index past the removed models.
          let offset = 0;
          for (let i = 0; i < idx; i++) offset += current[i]?.models?.length || 0;
          const removedCount = current[idx]?.models?.length || 0;
          let activeIdx = getState().activeModelIndex;
          if (activeIdx >= offset + removedCount) activeIdx -= removedCount;
          else if (activeIdx >= offset) activeIdx = Math.max(0, offset - 1);
          current.splice(idx, 1);
          setProviders(current);
          setModelConfigs(getState().modelConfigs.filter((_, i) => i < offset || i >= offset + removedCount), activeIdx);
          send({ type: "update_providers", providers: current, active_model_index: activeIdx });
        });
        return;
      }

      const editBtn = target.closest("[data-action='edit']");
      if (editBtn) {
        const idx = parseInt(editBtn.getAttribute("data-idx") || "0");
        this.showProviderEdit(idx);
        return;
      }

      const createBtn = target.closest("#btn-goto-create-provider");
      if (createBtn) {
        this.showProviderCreate();
        return;
      }
    });

    // ── Provider master switch (model table 操作 column) ──────────────
    // Turning a provider off makes every one of its models unavailable
    // WITHOUT touching the individual per-model switches, so switching it
    // back on restores exactly the previous selection.  Saves immediately.
    this.panels.model.addEventListener("change", (e) => {
      const input = (e.target as HTMLElement).closest(".provider-enabled-toggle") as HTMLInputElement | null;
      if (!input) return;
      const idx = parseInt(input.getAttribute("data-idx") || "-1");
      const current = [...(getState().providers || [])];
      if (idx < 0 || idx >= current.length) return;
      const next = current.map((p, i) => (i === idx ? { ...p, enabled: input.checked } : p));
      setProviders(next);

      // If the active model lived under the provider we just switched off,
      // move it to the first enabled model of the first provider still on.
      let activeIdx = getState().activeModelIndex;
      let offset = 0;
      for (let i = 0; i < idx; i++) offset += current[i]?.models?.length || 0;
      const count = current[idx]?.models?.length || 0;
      if (!input.checked && activeIdx >= offset && activeIdx < offset + count) {
        activeIdx = -1;
        let n = 0;
        for (let i = 0; i < next.length; i++) {
          const prov = next[i];
          if (prov.enabled === false) continue;
          const j = (prov.models || []).findIndex((m: any) => m.enabled !== false);
          if (j >= 0) { activeIdx = n + j; break; }
          n += prov.models?.length || 0;
        }
        if (activeIdx < 0) activeIdx = 0;
      }

      send({ type: "update_providers", providers: next, active_model_index: activeIdx });
      this.renderModel();
    });

    // ── MCP panel: create, edit, delete, toggle ─────────────────────
    this.panels.mcp.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;

      const deleteBtn = target.closest("[data-action='delete-mcp']");
      if (deleteBtn) {
        const idx = parseInt(deleteBtn.getAttribute("data-idx") || "0");
        const current = [...(getState().mcpServers || [])];
        const srv = current[idx];
        const name = srv?.name || t("settings.mcpServer");
        Dialog.confirm(t("settings.confirmDeleteMcpTitle"), t("settings.confirmDeleteMcp", { name }), { scope: "panel" }).then((ok) => {
          if (ok) {
            current.splice(idx, 1);
            setMcpServers(current);
            send({ type: "update_mcp", mcp_servers: current });
          }
        });
        return;
      }

      const editBtn = target.closest("[data-action='edit-mcp']");
      if (editBtn) {
        const idx = parseInt(editBtn.getAttribute("data-idx") || "0");
        this._renderMcpImportDialog(idx);
        return;
      }

      const createBtn = target.closest("#btn-goto-create-mcp");
      if (createBtn) {
        this._renderMcpImportDialog();
        return;
      }
    });

    this.panels.mcp.addEventListener("change", (e) => {
      const cb = (e.target as HTMLInputElement).closest(".mcp-enable-toggle") as HTMLInputElement | null;
      if (cb) {
        const idx = parseInt(cb.getAttribute("data-idx") || "0");
        const current = [...(getState().mcpServers || [])];
        if (idx >= 0 && idx < current.length) {
          current[idx] = { ...current[idx], disabled: !cb.checked };
          send({ type: "update_mcp", mcp_servers: current });
        }
      }
    });

    // ── Agent panel: create, edit, delete ────────────────────────────
    this.panels.agent.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;

      const createBtn = target.closest("#btn-create-agent");
      if (createBtn) {
        this.showAgentCreate();
        return;
      }

      const editBtn = target.closest("[data-action='edit']");
      if (editBtn && this.panels.agent.contains(editBtn)) {
        const idx = parseInt(editBtn.getAttribute("data-index") || "0");
        const agents = getState().subAgents || [];
        if (idx >= 0 && idx < agents.length) {
          this.showAgentCreate(agents[idx]);
        }
        return;
      }

      const deleteBtn = target.closest("[data-action='delete']");
      if (deleteBtn && this.panels.agent.contains(deleteBtn)) {
        const idx = parseInt(deleteBtn.getAttribute("data-index") || "0");
        const agents = getState().subAgents || [];
        if (idx < 0 || idx >= agents.length) return;
        const name = agents[idx].name;
        Dialog.confirm(t("settings.confirmDeleteSubAgent", { name }), t("settings.confirmDeleteSubAgentTitle"), { scope: "panel" }).then((confirmed) => {
          if (!confirmed) return;
          const updated = agents.filter((_, i) => i !== idx);
          setSubAgents(updated);
          send({ type: "update_sub_agents", agents: updated });
        });
        return;
      }
    });

    // ── Memory panel: the graph owns the panel and wires its own node clicks
    //    (renderMemory → createMemoryGraph.onSelect → sub-page reader).  A
    //    `view-memory` row can still jump straight to one node's reader. ───
    this.panels.memory.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;

      const viewBtn = target.closest("[data-action='view-memory']");
      if (viewBtn) {
        const path = viewBtn.getAttribute("data-path") || "";
        if (path) this._openMemoryEditor(path);
        return;
      }
    });

    onLocaleChange(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      this.refreshCurrentPanel();
    });

    // Auto-refresh the skills + commands panel when either list is replaced
    // (skill install/uninstall/edit, or command add/edit/delete).  The backend
    // swaps each slice for a fresh array, so a reference check catches adds,
    // removes and in-place edits alike — a length check would miss the latter.
    let lastSkillsRef = getState().skillsList;
    let lastCommandsRef = getState().customCommands;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const st = getState();
      if (st.skillsList !== lastSkillsRef || st.customCommands !== lastCommandsRef) {
        lastSkillsRef = st.skillsList;
        lastCommandsRef = st.customCommands;
        // Always re-render in case the user navigates there next.
        if (this.panels.skills) this.renderSkills();
      }
    }, ["skillsList", "customCommands"]);

    // Auto-refresh model panel when modelConfigs or activeModelIndex change
    let lastModelIdx = getState().activeModelIndex;
    let lastModelsRef = getState().modelConfigs;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const st = getState();
      const idxChanged = st.activeModelIndex !== lastModelIdx;
      // modelConfigs is replaced with a new array reference by setModelConfigs,
      // so a reference inequality detects enable/disable (and edit/delete) — not
      // just activeModelIndex changes.  Without this, disabling the last active
      // model (where activeModelIndex stays put) left the stale "使用中" tag.
      const modelsChanged = st.modelConfigs !== lastModelsRef;
      if (idxChanged || modelsChanged) {
        lastModelIdx = st.activeModelIndex;
        lastModelsRef = st.modelConfigs;
        if (this.currentPanel === "model" && this.panels.model) this.renderModel();
      }
    }, ["activeModelIndex", "modelConfigs"]);

    // Auto-refresh MCP panel when mcpServers change
    let lastMcpLen = getState().mcpServers.length;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const cur = getState().mcpServers.length;
      if (cur !== lastMcpLen) {
        lastMcpLen = cur;
        if (this.currentPanel === "mcp" && this.panels.mcp) this.renderMcpList();
      }
    }, ["mcpServers"]);

    // Auto-refresh agent panel when subAgents change
    let lastAgentLen = getState().subAgents.length;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const cur = getState().subAgents.length;
      if (cur !== lastAgentLen) {
        lastAgentLen = cur;
        if (this.currentPanel === "agent" && this.panels.agent) this.renderAgent();
      }
    }, ["subAgents"]);

    // Auto-refresh the memory graph only when the payload itself is replaced
    // (reference check).  profile_updated / memory_list churn must NOT
    // rebuild the view: a rebuild resets pan/zoom and used to wipe in-flight
    // node drags -- the "page refreshed itself" complaint.  The graph now
    // lives inside the memory editor card, so updates only flow while the
    // card is open; the panel itself no longer paints a graph.
    let lastMemoryGraphRef = getState().memoryGraph;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      if (getState().memoryGraph !== lastMemoryGraphRef) {
        lastMemoryGraphRef = getState().memoryGraph;
        if (this.currentPanel !== "memory" || !this._memoryGraphCtl || this._memoryGraphCtl.isInteracting()) return;
        // Reader sub-page open (graph view is display:none): applying the
        // push now would rebuild the canvas right before the user returns
        // ("refreshed itself").  The next push after the way back repaints.
        const graphView = document.getElementById("mem-graph-view");
        if (graphView && !graphView.offsetParent) return;
        // Graph data changed (e.g. a node's use count ticked after the
        // user opened a detail view).  update() keeps pan/zoom and dragged
        // positions, so returning from the markdown reader no longer looks
        // like the page refreshed itself.
        this._memoryGraphCtl.update(getState().memoryGraph || { nodes: [], edges: [] });
      }
    }, ["memoryGraph"]);

    // Auto-refresh the rules panel when global/project rules are replaced
    // (reference check catches edits, not just count changes).
    let lastGlobalRulesRef = getState().globalRules;
    let lastProjectRulesRef = getState().projectRules;
    let lastViewingRule = getState().viewingGlobalRule;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const st = getState();
      if (st.globalRules !== lastGlobalRulesRef || st.projectRules !== lastProjectRulesRef) {
        lastGlobalRulesRef = st.globalRules;
        lastProjectRulesRef = st.projectRules;
        if (this.currentPanel === "skills" && this.panels.skills) {
          this.renderRules();
        }
      }
      if (st.viewingGlobalRule !== lastViewingRule && st.viewingGlobalRule) {
        lastViewingRule = st.viewingGlobalRule;
        if (this.currentPanel === "skills" && !st.viewingGlobalRule.error) {
          this._showRuleFormDialog(
            st.viewingGlobalRule.name,
            st.viewingGlobalRule.content,
            true
          );
        }
      }
    }, ["globalRules", "projectRules", "viewingGlobalRule"]);

    // Auto-refresh usage panel when usageStats changes
    let lastUsageStats = getState().usageStats;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const cur = getState().usageStats;
      if (cur !== lastUsageStats) {
        lastUsageStats = cur;
        if (this.currentPanel === "usage") void this._renderUsageSection();
      }
    }, ["usageStats"]);

    // Auto-refresh gateway panel when gatewayStatus changes
    let lastGatewayStatus = getState().gatewayStatus;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const cur = getState().gatewayStatus;
      if (cur !== lastGatewayStatus) {
        lastGatewayStatus = cur;
        if (this.currentPanel === "gateway" && this.panels.gateway) {
          scheduleRender("settings:gateway", () => this.renderGateway(), 60);
        }
      }
    }, ["gatewayStatus"]);

    // Fill memory detail dialog when content arrives
    let lastMemoryDetail = getState().memoryDetail;
    subscribe(() => {
      const curr = getState().memoryDetail;
      if (curr !== lastMemoryDetail) {
        lastMemoryDetail = curr;
        const contentEl = document.getElementById("memory-detail-content");
        if (contentEl) {
          // The branded loader belongs to the dialog lifetime: drop it as
          // soon as real content (or an error) takes over the body.
          this._destroyMemoryDetailLoader();
          if (curr?.error) {
            contentEl.innerHTML = `<span class="error-text">Error: ${this.esc(curr.error)}</span>`;
          } else if (curr?.content) {
            // Detail layers start with a YAML frontmatter block (machine
            // metadata: name/type/tags/description).  Rendering it produces
            // a stray <hr> (the opening --- delimiter) followed by raw
            // key-value text before the real body -- strip the whole block
            // so only the authored markdown shows.
            const frontmatter = /^---\r?\n[\s\S]*?\r?\n---\s*(?:\r?\n|$)/;
            contentEl.innerHTML = renderMarkdown(curr.content.replace(frontmatter, ""));
            // Code-block copy buttons are lucide placeholders until rasterised.
            if (typeof (window as any).lucide !== "undefined") {
              (window as any).lucide.createIcons({ root: contentEl });
            }
          } else {
            this._showMemoryDetailLoading(contentEl);
          }
        }
      }
    }, ["memoryDetail"]);

    // Handle WeChat QR code scan results (adapter-config dialog elements)
    onWechatScanResult((event) => {
      if (!(this as any)._wechatDialogOpen) return;
      const img = document.getElementById("dlg-weixin-qr-img") as HTMLImageElement | null;
      const statusEl = document.getElementById("dlg-weixin-qr-status");
      const countdownEl = document.getElementById("dlg-weixin-qr-countdown");
      const successOverlay = document.getElementById("dlg-weixin-qr-success");
      const refreshOverlay = document.getElementById("dlg-weixin-qr-refresh");
      if (event.scan_confirmed) {
        if ((this as any)._qrCountdown) clearInterval((this as any)._qrCountdown);
        if (countdownEl) { countdownEl.style.display = "none"; countdownEl.textContent = ""; }
        if (statusEl) {
          // The success message names the bound platform ("{name}"), which
          // comes from the adapter id captured when the dialog opened.
          const qrAdapterId = ((this as any)._qrAdapterId as string) || "weixin";
          const platformName = t(`settings.adapterName${qrAdapterId.charAt(0).toUpperCase() + qrAdapterId.slice(1)}`);
          statusEl.textContent = t("settings.wechatScanSuccess").replace("{name}", platformName);
          statusEl.style.display = "block";
        }
        if (successOverlay) successOverlay.style.display = "flex";
        // Save credentials to local state so the card shows correct status.
        // Credential keys are platform-specific: QQ Bot carries app_id /
        // app_secret from the bind task, WeCom carries bot_id / secret,
        // WeChat carries iLink credentials.
        if (event.credentials) {
          const creds = event.credentials;
          const wecomBind = creds.bot_id !== undefined;
          const qqBind = creds.app_id !== undefined;
          const current = wecomBind
            ? { ...getState().settings,
                adapter_wecom_enabled: true,
                adapter_wecom_bot_id: creds.bot_id || "",
                adapter_wecom_secret: creds.secret || "",
              }
            : qqBind
            ? { ...getState().settings,
                adapter_qqbot_enabled: true,
                adapter_qqbot_app_id: creds.app_id || "",
                adapter_qqbot_client_secret: creds.app_secret || "",
              }
            : { ...getState().settings,
                adapter_weixin_enabled: true,
                adapter_weixin_app_id: creds.ilink_bot_id || "",
                adapter_weixin_account_id: creds.ilink_bot_id || "",
                adapter_weixin_token: creds.bot_token || "",
                adapter_weixin_api_url: creds.baseurl || "",
                adapter_weixin_base_url: creds.baseurl || "",
              };
          setSettings(current as any);
        }
        // Auto-close dialog after 1.5s and refresh the card
        setTimeout(() => {
          (this as any)._wechatDialogOpen = false;
          const closeFn = (this as any)._closeWeixinDialog;
          if (typeof closeFn === "function") closeFn();
          if (this.currentPanel === "gateway") this.renderGateway();
        }, 1500);
        return;
      }
      // Scanned, confirmation pending on the phone: stop the countdown and
      // keep waiting — the QR itself stays valid until confirmed.
      if (event.scan_detected) {
        if ((this as any)._qrCountdown) clearInterval((this as any)._qrCountdown);
        if (countdownEl) { countdownEl.style.display = "none"; countdownEl.textContent = ""; }
        if (statusEl) {
          statusEl.textContent = t("settings.wechatScanDetected");
          statusEl.style.display = "block";
        }
        return;
      }
      // Server-side expiry (QQ bind task): same UI as the countdown reaching
      // zero — show the expired status and the refresh overlay.
      if (event.qr_expired) {
        if ((this as any)._qrCountdown) clearInterval((this as any)._qrCountdown);
        if (countdownEl) { countdownEl.style.display = "none"; countdownEl.textContent = ""; }
        if (statusEl) {
          statusEl.textContent = t("settings.wechatQrExpired");
          statusEl.style.display = "block";
        }
        if (refreshOverlay) refreshOverlay.style.display = "flex";
        return;
      }
      if (event.success && event.qrcode_url) {
        if ((this as any)._qrResultReceived) return;
        (this as any)._qrResultReceived = true;
        if (statusEl) { statusEl.style.display = "none"; statusEl.textContent = ""; }
        if (img) {
          img.src = event.qrcode_url;
          img.style.display = "block";
        }
        if (countdownEl) {
          countdownEl.style.display = "block";
          countdownEl.textContent = t("settings.wechatRemainingTime").replace("{seconds}", "120");
          let remain = 120;
          if ((this as any)._qrCountdown) clearInterval((this as any)._qrCountdown);
          (this as any)._qrCountdown = setInterval(() => {
            remain -= 1;
            const el = document.getElementById("dlg-weixin-qr-countdown");
            if (!el) {
              clearInterval((this as any)._qrCountdown);
              return;
            }
            if (remain <= 0) {
              el.textContent = "";
              el.style.display = "none";
              const statusEl = document.getElementById("dlg-weixin-qr-status");
              if (statusEl) {
                statusEl.textContent = t("settings.wechatQrExpired");
                statusEl.style.display = "block";
              }
              const refreshOverlay = document.getElementById("dlg-weixin-qr-refresh");
              if (refreshOverlay) refreshOverlay.style.display = "flex";
              clearInterval((this as any)._qrCountdown);
              return;
            }
            el.textContent = t("settings.wechatRemainingTime").replace("{seconds}", String(remain));
          }, 1000);
        }
      } else {
        if (img) img.style.display = "none";
        if (countdownEl) { countdownEl.style.display = "none"; countdownEl.textContent = ""; }
        if (refreshOverlay) refreshOverlay.style.display = "none";
        if (statusEl) {
          statusEl.style.display = "block";
          statusEl.textContent = event.message || t("settings.wechatScanning");
        }
      }
    });
}
