/**
 * The Encre Agent Desktop Subwindow's (ESD) own views — everything that is
 * neither a web page nor a file, so no file-pipeline tab type covers it:
 * jurisdiction-aware legal documents, the license text, the data center and
 * the nebula easter egg.
 *
 * The unified tab layer (`session_inner`) owns the tab strip and the panel
 * skeleton.  This module only paints inside the host element it is handed and
 * always returns a teardown, so closing a tab leaves no listener, poller or
 * animation frame behind.
 */

import { t, getLocale } from "./i18n.js";
import { openRoutedLink } from "../core/state.js";
import { MarkdownPreviewView } from "../chat/markdown_preview.js";
import { EALoader } from "./ealoader.js";
import { mountDataCenter } from "./datacenter/index.js";
import { mountNebula } from "./easter-egg.js";
import { bindDropdownImpl, renderDropdownImpl, type DropdownOption } from "../ui/dropdown.js";

/** Views routed here instead of to a URL. Must stay in sync with
 *  `LEGAL_DOC_FILES` in main.ts; `thanks` has no per-region variants but
 *  reuses the same viewer. */
const SUBAPP_VIEWS: ReadonlySet<string> = new Set([
  "license",
  "privacy",
  "terms",
  "agreement",
  "content-rules",
  "minors",
  "data-rules",
  "thanks",
  "data-center",
  "easter-egg",
]);

/** The glyph the app already uses for each view (the About rows list the same
 *  legal documents with these icons), so an ESD tab matches its entry point. */
const SUBAPP_TAB_ICONS: Record<string, string> = {
  license: "file-text",
  privacy: "shield",
  terms: "scroll",
  agreement: "file-signature",
  "content-rules": "book-open-check",
  thanks: "heart",
  "data-rules": "database",
  minors: "shield",
  "data-center": "hard-drive",
  "easter-egg": "sparkles",
};

/** Every jurisdiction the legal documents ship for, with its label key. */
const DOC_REGION_LABEL_KEYS: Record<string, string> = {
  // Americas
  us: "settings.aboutRegionUs",
  ca: "settings.aboutRegionCa",
  mx: "settings.aboutRegionMx",
  br: "settings.aboutRegionBr",
  // Europe
  eu: "settings.aboutRegionEu",
  uk: "settings.aboutRegionUk",
  ch: "settings.aboutRegionCh",
  tr: "settings.aboutRegionTr",
  // Asia-Pacific
  cn: "settings.aboutRegionCn",
  jp: "settings.aboutRegionJp",
  kr: "settings.aboutRegionKr",
  tw: "settings.aboutRegionTw",
  hk: "settings.aboutRegionHk",
  mo: "settings.aboutRegionMo",
  sg: "settings.aboutRegionSg",
  in: "settings.aboutRegionIn",
  // Middle East & Africa
  ae: "settings.aboutRegionAe",
  sa: "settings.aboutRegionSa",
  il: "settings.aboutRegionIl",
  za: "settings.aboutRegionZa",
  ng: "settings.aboutRegionNg",
  ke: "settings.aboutRegionKe",
  eg: "settings.aboutRegionEg",
  // Oceania
  au: "settings.aboutRegionAu",
  nz: "settings.aboutRegionNz",
};

export function isSubappView(view: string): boolean {
  return SUBAPP_VIEWS.has(view);
}

export function subappTabIcon(view: string): string {
  return SUBAPP_TAB_ICONS[view] || "square";
}

/** Best-effort geo region from the OS timezone (the machine's real physical
 *  environment — no IP lookups). Returns "" when the timezone is unknown. */
function timezoneDocRegion(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    const map: ReadonlyArray<readonly [RegExp, string]> = [
      [/^Asia\/(Shanghai|Urumqi|Chongqing|Harbin|Kashgar)/, "cn"],
      [/^Asia\/Tokyo$/, "jp"],
      [/^Asia\/(Seoul|Pyongyang)$/, "kr"],
      [/^Asia\/Taipei$/, "tw"],
      [/^Asia\/Hong_Kong$/, "hk"],
      [/^Asia\/Macau$/, "mo"],
      [/^Asia\/Singapore$/, "sg"],
      [/^Asia\/(Kolkata|Calcutta|Colombo)$/, "in"],
      [/^Asia\/(Dubai|Muscat|Abu_Dhabi)$/, "ae"],
      [/^Asia\/(Riyadh|Qatar|Kuwait|Bahrain)$/, "sa"],
      [/^Asia\/(Jerusalem|Tel_Aviv|Gaza|Hebron)$/, "il"],
      [/^Europe\/(London|Belfast)$/, "uk"],
      [/^Europe\/(Zurich|Geneva)$/, "ch"],
      [/^Europe\/Istanbul$/, "tr"],
      [/^America\/(New_York|Chicago|Denver|Los_Angeles|Anchorage|Phoenix|Detroit|Honolulu|Boise|Indianapolis|Juneau)$/, "us"],
      [/^America\/(Toronto|Vancouver|Winnipeg|Halifax|St_Johns|Edmonton|Regina|Iqaluit)$/, "ca"],
      [/^America\/(Mexico_City|Monterrey|Tijuana|Cancun|Merida)$/, "mx"],
      [/^America\/(Sao_Paulo|Bahia|Fortaleza|Manaus|Rio_Branco|Recife|Araguaina)$/, "br"],
      [/^Australia\/(Sydney|Melbourne|Brisbane|Perth|Adelaide|Hobart|Darwin)$/, "au"],
      [/^Pacific\/(Auckland|Fiji)$/, "nz"],
      [/^Africa\/(Johannesburg|Cape_Town)$/, "za"],
      [/^Africa\/(Lagos|Abuja)$/, "ng"],
      [/^Africa\/Nairobi$/, "ke"],
      [/^Africa\/Cairo$/, "eg"],
    ];
    for (const [re, region] of map) if (re.test(tz)) return region;
  } catch {}
  return "";
}

/** Default legal-doc region: OS timezone geo first (real physical location),
 *  then the UI locale as a weak fallback (used before the user picks one). */
function defaultDocRegion(locale: string): string {
  const tz = timezoneDocRegion();
  if (tz) return tz;
  switch (locale) {
    case "zh": return "cn";
    case "zh-Hant": return "tw";
    case "ja": return "jp";
    case "ko": return "kr";
    case "de": return "ch";
    case "tr": return "tr";
    case "es": return "mx";
    case "pt": return "br";
    case "ar": return "ae";
    case "he": return "il";
    default: return "us";
  }
}

/** The region the open documents use. Main holds the durable copy; this cache
 *  only keeps several doc tabs in one window on the same jurisdiction. */
let _region = "";

async function resolveDocRegion(): Promise<string> {
  if (_region) return _region;
  const fallback = defaultDocRegion(getLocale());
  try {
    const saved = window.electronAPI ? await window.electronAPI.getDocRegion() : "";
    _region = saved && DOC_REGION_LABEL_KEYS[saved] ? saved : fallback;
  } catch {
    _region = fallback;
  }
  return _region;
}

const PANEL_STYLE = "display:flex;flex-direction:column;flex:1;min-height:0;height:100%;";

/** Paint `view` inside `panel` (a `.tab-panel`). Returns its teardown.
 *  The panel's own `display` is left alone on purpose: `.tab-panel` /
 *  `.tab-panel.active` is what shows and hides tabs, and an inline style here
 *  would keep an inactive view painted on top of the active one. */
export function mountSubappView(view: string, panel: HTMLElement): () => void {
  const host = document.createElement("div");
  host.style.cssText = PANEL_STYLE;
  panel.appendChild(host);
  if (view === "easter-egg") return mountEasterEgg(host);
  if (view === "data-center") {
    const handle = mountDataCenter(host);
    return () => handle.destroy();
  }
  if (view === "license") return mountLicense(host);
  return mountLegalDoc(view, host);
}

function mountEasterEgg(host: HTMLElement): () => void {
  host.style.overflow = "hidden";
  host.style.background = "#000";
  const stage = document.createElement("div");
  stage.style.cssText = "position:relative;flex:1;min-height:0;height:100%;overflow:hidden";
  host.appendChild(stage);
  return mountNebula(stage);
}

function mountLicense(host: HTMLElement): () => void {
  const scroll = document.createElement("div");
  scroll.className = "child-view-content";
  const pre = document.createElement("pre");
  pre.className = "child-raw-text";
  scroll.appendChild(pre);
  host.appendChild(scroll);
  EALoader.mountCentered(scroll, "96px");
  const api = window.electronAPI;
  if (api) {
    void api.getLicenseContent().then((content) => {
      EALoader.destroyMounted(scroll);
      pre.textContent = content;
    });
  }
  return () => {
    EALoader.destroyMounted(scroll);
    scroll.remove();
  };
}

function mountLegalDoc(view: string, host: HTMLElement): () => void {
  const wrap = document.createElement("div");
  wrap.className = "child-doc-container";
  wrap.innerHTML = `
    <div class="child-doc-toolbar"></div>
    <div class="child-doc-body"></div>`;
  host.appendChild(wrap);

  const bodyHost = wrap.querySelector<HTMLElement>(".child-doc-body")!;
  const toolbar = wrap.querySelector<HTMLElement>(".child-doc-toolbar")!;
  const preview = new MarkdownPreviewView(bodyHost, "", { onOpenLink: openRoutedLink });
  const mdBody = bodyHost.querySelector<HTMLElement>(".markdown-preview-body")!;
  EALoader.mountCentered(mdBody, "96px");

  // Each document gets its own dropdown id: several legal tabs are open at the
  // same time in one window and the dropdown helpers address elements by id.
  const dropdownId = `doc-region-${view.replace(/[^a-z0-9-]/gi, "-")}`;
  const options: DropdownOption[] = Object.entries(DOC_REGION_LABEL_KEYS).map(([id, key]) => ({ id, label: t(key) }));

  const load = (region: string) => {
    EALoader.mountCentered(mdBody, "96px");
    const api = window.electronAPI;
    if (!api) return;
    void api.getDocumentContent(view, region).then((content) => {
      preview.setContent(content);
    });
  };

  const onRegion = (region: string) => {
    _region = region;
    void window.electronAPI?.setDocRegion?.(region);
    load(region);
  };

  void resolveDocRegion().then((region) => {
    toolbar.innerHTML = renderDropdownImpl(dropdownId, options, region, onRegion);
    bindDropdownImpl(dropdownId, onRegion);
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: toolbar });
    }
    load(region);
  });

  return () => {
    preview.destroy();
    wrap.remove();
  };
}
