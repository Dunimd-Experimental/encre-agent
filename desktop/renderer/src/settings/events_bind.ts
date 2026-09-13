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
import { setModelConfigs, setMcpServers, setSkillsList, setSubAgents } from "../core/state.js";
import type { ModelConfigMeta, MCPServerConfig, SkillInfo, ModelCatalog, McpCatalog, McpProviderEntry, ProviderEntry, ProfileData, CustomCommand, UsageStatsSessionEntry } from "../core/types.js";
import { defaultSearchFilter } from "../core/types.js";
import { Dialog } from "../ui/dialog.js";
import {
  bindTargetModelSelection,
  parseTargetModelSelection,
  readTargetModelSelection,
  renderTargetModelSelection,
  serializeTargetModelSelection,
} from "./model-selection.js";
import { t, initLocale, setLocale, getLocale, getIntlLocale, clearLocaleCache, onLocaleChange, LOCALES, type Locale } from "../features/i18n.js";
import { applyServerCommands } from "../features/slash_commands.js";
import { renderMarkdown } from "../chat/chat.js";
import { platformIconHtml, searchEngineIconSrc } from "../ui/icons.js";
import { formatShortcut } from "../features/shortcutDisplay.js";
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

    // Index panel: document add/remove
    this.panels.index.addEventListener("click", (e) => {
      console.log("[DEBUG index] click fired, target:", (e.target as HTMLElement).className, "tag:", (e.target as HTMLElement).tagName);
      const target = e.target as HTMLElement;

      // Add document dropdown trigger
      const trigger = target.closest("#doc-add-trigger");
      if (trigger) {
        e.stopPropagation();
        const dd = document.getElementById("doc-add-dropdown");
        if (dd) {
          const isOpen = dd.classList.contains("open");
          document.querySelectorAll(".settings-dropdown.open").forEach((d) => d.classList.remove("open"));
          if (!isOpen) dd.classList.add("open");
        }
        return;
      }

      // Dropdown items (local file / URL)
      const item = target.closest(".settings-dropdown-item");
      if (item && document.getElementById("doc-add-dropdown")?.contains(item)) {
        document.getElementById("doc-add-dropdown")?.classList.remove("open");
        const action = item.getAttribute("data-action");
        if (action === "local") {
          (async () => {
            const api = (window as any).electronAPI;
            if (api?.pickFiles) {
              const paths = await api.pickFiles();
              if (!paths?.length) return;
              const filePath = paths[0];
              const fileName = filePath.split(/[/\\]/).pop() || filePath;
              this._showDocNameDialog(fileName, (name) => {
                send({ type: "add_document", name: name || fileName, file_path: filePath } as any);
              }, t);
            }
          })();
        } else if (action === "url") {
          this._showDocUrlDialog(t);
        }
        return;
      }

      // Remove document button
      const removeBtn = target.closest("[data-action='delete-doc']");
      if (removeBtn) {
        const id = removeBtn.getAttribute("data-doc-id");
        const name = removeBtn.getAttribute("data-doc-name");
        if (id && name) {
          Dialog.confirm(t("settings.deleteDoc"), t("settings.confirmDeleteDoc", { name }), { container: document.getElementById("settings-view") ?? undefined }).then((ok) => {
            if (ok) {
              send({ type: "remove_document", id } as any);
            }
          });
        }
        return;
      }
    });

    // Skills panel: skill view/edit/delete, command add/edit/delete
    this.panels.skills.addEventListener("click", (e) => {
      console.log("[DEBUG skills] click fired, target:", (e.target as HTMLElement).className, "tag:", (e.target as HTMLElement).tagName);
      const target = e.target as HTMLElement;

      // View skill
      const viewBtn = target.closest("[data-action='view-skill']");
      if (viewBtn) {
        console.log("[DEBUG skills] view-slick matched, name:", viewBtn.getAttribute("data-name"));
        const name = viewBtn.getAttribute("data-name") || "";
        if (name) this.showSkillDetail(name);
        return;
      }

      // Edit skill
      const editBtn = target.closest("[data-action='edit-skill']");
      if (editBtn) {
        console.log("[DEBUG skills] edit-skill matched, name:", editBtn.getAttribute("data-name"));
        const name = editBtn.getAttribute("data-name") || "";
        if (name) this.showSkillDetail(name, true);
        return;
      }

      // Delete skill
      const deleteBtn = target.closest("[data-action='delete-skill']");
      if (deleteBtn) {
        const name = deleteBtn.getAttribute("data-name") || "";
        if (name) {
          Dialog.confirm(t("settings.delete"), t("settings.skillDeleteConfirm", { name }), { container: document.getElementById("settings-view") ?? undefined }).then(async (ok) => {
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
          Dialog.confirm(t("settings.delete"), t("settings.commandRemoveConfirm", { name }), { container: document.getElementById("settings-view") ?? undefined }).then(async (ok) => {
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

    // ── Model panel: edit, delete, enable/disable ───────────────────
    this.panels.model.addEventListener("change", (e) => {
      const cb = (e.target as HTMLInputElement).closest(".model-enable-toggle") as HTMLInputElement | null;
      if (!cb) return;
      const idx = parseInt(cb.getAttribute("data-idx") || "0");
      const currentModels = [...getState().modelConfigs];
      if (idx < 0 || idx >= currentModels.length) return;

      const isMultimodal = cb.classList.contains("model-multimodal-toggle");
      if (isMultimodal) {
        currentModels[idx] = { ...currentModels[idx], multimodal: cb.checked };
      } else {
const newEnabled = cb.checked;
        currentModels[idx] = { ...currentModels[idx], enabled: newEnabled };
        let activeIdx = getState().activeModelIndex;
        if (!newEnabled && idx === activeIdx) {
          const nextIdx = currentModels.findIndex((m, i) => i !== idx && m.enabled !== false);
          if (nextIdx >= 0) activeIdx = nextIdx;
        }
setModelConfigs(currentModels, activeIdx);
        send({ type: "update_models", models: currentModels, active_model_index: activeIdx });
      }
    });

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
        const m = getState().modelConfigs[idx];
        Dialog.confirm(t("common.confirmDeleteTitle"), t("common.confirmDelete", { name: m?.name || t("common.unnamed") }), { container: document.getElementById("settings-view") ?? undefined }).then((ok) => {
          if (ok) {
            const currentModels = [...getState().modelConfigs];
            currentModels.splice(idx, 1);
            let activeIdx = getState().activeModelIndex;
            if (idx < activeIdx) activeIdx--;
            if (activeIdx >= currentModels.length) activeIdx = Math.max(0, currentModels.length - 1);
            setModelConfigs(currentModels, activeIdx);
            send({ type: "delete_model", model_index: idx });
          }
        });
        return;
      }

      const editBtn = target.closest("[data-action='edit']");
      if (editBtn) {
        const idx = parseInt(editBtn.getAttribute("data-idx") || "0");
        this.showModelEdit(idx);
        return;
      }

      const createBtn = target.closest("#btn-goto-create-model");
      if (createBtn) {
        this.showModelCreate();
        return;
      }
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
        Dialog.confirm(t("settings.confirmDeleteMcpTitle"), t("settings.confirmDeleteMcp", { name }), { container: document.getElementById("settings-view") ?? undefined }).then((ok) => {
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
        Dialog.confirm(t("settings.confirmDeleteSubAgent", { name }), t("settings.confirmDeleteSubAgentTitle"), { container: document.getElementById("settings-view") ?? undefined }).then((confirmed) => {
          if (!confirmed) return;
          const updated = agents.filter((_, i) => i !== idx);
          setSubAgents(updated);
          send({ type: "update_sub_agents", agents: updated });
        });
        return;
      }
    });

    // ── Memory panel: refresh, view ──────────────────────────────────
    this.panels.memory.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;

      const refreshBtn = target.closest("#btn-refresh-memory");
      if (refreshBtn) {
        refreshAllData();
        return;
      }

      const viewBtn = target.closest("[data-action='view-memory']");
      if (viewBtn) {
        const path = viewBtn.getAttribute("data-path") || "";
        if (path) this._showMemoryDetailDialog(path);
        return;
      }
    });

    onLocaleChange(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      this.refreshCurrentPanel();
    });

    // Auto-refresh skills panel when skillsList changes (install/uninstall)
    let lastSkillsLen = getState().skillsList.length;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const currentLen = getState().skillsList.length;
      if (currentLen !== lastSkillsLen) {
        lastSkillsLen = currentLen;
        // Always re-render skills list in case user navigates there
        if (this.panels.skills) this.renderSkills();
      }
    });

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
    });

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
    });

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
    });

    // Auto-refresh memory panel when memoryList or profile changes
    let lastMemoryLen = getState().memoryList.length;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      if (getState().memoryList.length !== lastMemoryLen) {
        lastMemoryLen = getState().memoryList.length;
        if (this.currentPanel === "memory" && this.panels.memory) this.renderMemory();
      }
    });
    let lastProfileUpd = getState().profile?.update_count ?? -1;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const currUpd = getState().profile?.update_count ?? -1;
      if (currUpd !== lastProfileUpd) {
        lastProfileUpd = currUpd;
        if (this.currentPanel === "memory" && this.panels.memory) this.renderMemory();
      }
    });

    // Auto-refresh index panel when docsList changes
    let lastDocsLen = getState().docsList.length;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      if (getState().docsList.length !== lastDocsLen) {
        lastDocsLen = getState().docsList.length;
        if (this.panels.index) this.renderIndex();
      }
    });

    // Auto-refresh rules panel when globalRules / projectRules changes
    let lastRulesLen = getState().globalRules.length;
    let lastProjectRulesLen = getState().projectRules.length;
    let lastViewingRule = getState().viewingGlobalRule;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const st = getState();
      if (st.globalRules.length !== lastRulesLen) {
        lastRulesLen = st.globalRules.length;
        if (this.currentPanel === "rules" && this.panels.rules) {
          this.renderRules();
        }
      }
      if (st.projectRules.length !== lastProjectRulesLen) {
        lastProjectRulesLen = st.projectRules.length;
        if (this.currentPanel === "rules" && this.panels.rules) {
          this.renderRules();
        }
      }
      if (st.viewingGlobalRule !== lastViewingRule && st.viewingGlobalRule) {
        const prev = lastViewingRule;
        lastViewingRule = st.viewingGlobalRule;
        if (this.currentPanel === "rules" && !st.viewingGlobalRule.error) {
          this._showRuleFormDialog(
            st.viewingGlobalRule.name,
            st.viewingGlobalRule.content,
            true
          );
        }
      }
    });

    // Auto-refresh permissions panel when policies change
    let lastPermissionPolicies = JSON.stringify(getState().permissionPolicies);
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const current = JSON.stringify(getState().permissionPolicies);
      if (current !== lastPermissionPolicies) {
        lastPermissionPolicies = current;
        if (this.currentPanel === "permissions" && this.panels.permissions) {
          this.renderPermissions();
        }
      }
    });

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
    });

    // Auto-refresh gateway panel when gatewayStatus changes
    let lastGatewayStatus = getState().gatewayStatus;
    subscribe(() => {
      const app = document.getElementById("app");
      if (!app?.classList.contains("settings-mode")) return;
      const cur = getState().gatewayStatus;
      if (cur !== lastGatewayStatus) {
        lastGatewayStatus = cur;
        if (this.currentPanel === "gateway" && this.panels.gateway) requestAnimationFrame(() => this.renderGateway());
      }
    });

    // Fill memory detail dialog when content arrives
    let lastMemoryDetail = getState().memoryDetail;
    subscribe(() => {
      const curr = getState().memoryDetail;
      if (curr !== lastMemoryDetail) {
        lastMemoryDetail = curr;
        const contentEl = document.getElementById("memory-detail-content");
        if (contentEl) {
          if (curr?.error) {
            contentEl.innerHTML = `<span class="error-text">Error: ${this.esc(curr.error)}</span>`;
          } else if (curr?.content) {
            contentEl.innerHTML = renderMarkdown(curr.content);
          } else {
            contentEl.textContent = t("settings.loading");
          }
        }
      }
    });

    // Handle WeChat QR code scan results
    onWechatScanResult((event) => {
      if (!(this as any)._wechatDialogOpen) return;
      const img = document.getElementById("wechat-qr-img") as HTMLImageElement | null;
      const statusEl = document.getElementById("wechat-qr-status");
      const countdownEl = document.getElementById("wechat-qr-countdown");
      const scanBtn = document.getElementById("wechat-scan-btn");
      if (scanBtn) scanBtn.removeAttribute("disabled");
      if (event.scan_confirmed) {
        if ((this as any)._qrCountdown) clearInterval((this as any)._qrCountdown);
        if (countdownEl) { countdownEl.style.display = "none"; countdownEl.textContent = ""; }
        if (statusEl) {
          statusEl.textContent = t("settings.wechatScanSuccess");
          statusEl.style.padding = "12px 0 0";
          statusEl.style.display = "block";
        }
        const successOverlay = document.getElementById("wechat-qr-success-overlay");
        if (successOverlay) {
          successOverlay.style.display = "flex";
          // Shared one-shot entrance (see `.fade-in-once` in styles.css) —
          // the class comes back off on animationend so a later reveal can
          // replay it.
          successOverlay.classList.add("fade-in-once");
          successOverlay.addEventListener(
            "animationend",
            () => successOverlay.classList.remove("fade-in-once"),
            { once: true },
          );
        }
        // Save credentials to local state so the card shows correct status
        if (event.credentials) {
          const current = { ...getState().settings,
            adapter_weixin_enabled: true,
            adapter_weixin_app_id: event.credentials.ilink_bot_id || "",
            adapter_weixin_token: event.credentials.bot_token || "",
            adapter_weixin_api_url: event.credentials.baseurl || "",
          };
          setSettings(current as any);
        }
        // Auto-close dialog after 1.5s and refresh the card
        setTimeout(() => {
          (this as any)._wechatDialogOpen = false;
          document.getElementById("wechat-qr-overlay")?.remove();
          if (this.currentPanel === "gateway") this.renderGateway();
        }, 1500);
        return;
      }
      if (event.success && event.qrcode_url) {
        if ((this as any)._qrResultReceived) return;
        (this as any)._qrResultReceived = true;
        if (statusEl) { statusEl.style.display = "none"; statusEl.style.padding = "60px 0 0"; }
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
            const el = document.getElementById("wechat-qr-countdown");
            if (!el) {
              clearInterval((this as any)._qrCountdown);
              return;
            }
            if (remain <= 0) {
              el.textContent = "";
              el.style.display = "none";
              const statusEl = document.getElementById("wechat-qr-status");
              if (statusEl) {
                statusEl.textContent = t("settings.wechatQrExpired");
                statusEl.style.padding = "12px 0 0";
                statusEl.style.display = "block";
              }
              const refreshOverlay = document.getElementById("wechat-qr-refresh-overlay");
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
        if (statusEl) {
          statusEl.style.display = "flex";
          statusEl.style.flexDirection = "column";
          statusEl.style.alignItems = "center";
          statusEl.style.justifyContent = "center";
          statusEl.style.gap = "8px";
          statusEl.style.padding = "40px 0";
          statusEl.style.color = "var(--text-muted)";
          statusEl.innerHTML = `
            <i data-lucide="scan-line" style="width:32px;height:32px;opacity:0.3"></i>
            <span style="font-size:13px;opacity:0.7">${event.message || t("settings.wechatScanning")}</span>`;
          if (typeof (window as any).lucide !== "undefined") {
            (window as any).lucide.createIcons({ root: statusEl });
          }
        }
      }
    });
}
