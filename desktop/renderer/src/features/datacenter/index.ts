/**
 * Data center — the Encre Agent Desktop Subwindow (ESD) page that shows and manages everything under the Encre
 * data root.
 *
 * Entry point: `mountDataCenter(container)` is called from `app.ts` when a tab
 * whose view id is `data-center` is rendered — the same tier as `logs` or a
 * legal document, not a webview.
 *
 * Layout of this feature:
 *
 * | file          | responsibility                                        |
 * | ------------- | ----------------------------------------------------- |
 * | `types.ts`    | domain types + the `DataProvider` contract            |
 * | `registry.ts` | what data *can* exist (categories + module metadata)  |
 * | `provider.ts` | what data *does* exist (survey from the core server)  |
 * | `pie.ts`      | the donut chart and its hover behaviour               |
 * | `home.ts`     | landing page: category tiles + chart                  |
 * | `category.ts` | one category's module list (drill-in page)            |
 * | `index.ts`    | mounting + navigation state                           |
 *
 * Navigation mirrors the automation sub-pages: the home page and the category
 * page swap wholesale inside the tab, and the breadcrumb's root is the way
 * back. Reached from 设置 → 数据 → 数据中心.
 */

import { onLocaleChange } from "../i18n.js";
import { CategoryPage } from "./category.js";
import { HomeView } from "./home.js";
import { ModulePage } from "./module.js";
import { CoreProvider } from "./provider.js";
import type { DataProvider } from "./types.js";

/** Returned by {@link mountDataCenter} so the tab can tear the page down. */
export interface DataCenterHandle {
  destroy(): void;
}

/** Statistics are served by core; the renderer never scans the disk itself. */
function createProvider(): DataProvider {
  return new CoreProvider();
}

function paintIcons(root: HTMLElement): void {
  const lucide = (window as unknown as { lucide?: { createIcons?: (o: { root: HTMLElement }) => void } }).lucide;
  lucide?.createIcons?.({ root });
}

/**
 * Build the data center inside *container*.
 *
 * The container is the tab's `.tab-panel`; its previous content is discarded.
 */
export function mountDataCenter(container: HTMLElement): DataCenterHandle {
  const root = document.createElement("div");
  root.className = "dc-root";
  container.appendChild(root);

  const provider = createProvider();

  // --- navigation ---------------------------------------------------------
  // Only one page exists at a time: home, a category page, or a module page.
  let home: HomeView | null = null;
  let category: CategoryPage | null = null;
  let module: ModulePage | null = null;

  const teardown = (): void => {
    home?.destroy();
    home = null;
    category?.root.remove();
    category = null;
    module?.destroy();
    module = null;
  };

  const showHome = (): void => {
    teardown();
    home = new HomeView(root, {
      provider,
      onOpen: showCategory,
    });
    home.refresh();
    paintIcons(root);
  };

  const showCategory = (categoryId: string): void => {
    teardown();
    category = new CategoryPage(root, {
      provider,
      categoryId,
      onBack: showHome,
      onOpenModule: showModule,
    });
    paintIcons(root);
  };

  const showModule = (categoryId: string, moduleId: string): void => {
    teardown();
    module = new ModulePage(root, {
      provider,
      categoryId,
      moduleId,
      onBack: () => showCategory(categoryId),
      onRoot: showHome,
    });
    paintIcons(root);
  };

  showHome();

  // Statistics come from core asynchronously; repaint once they land (or
  // fail — the page stays at zero instead of inventing numbers).
  void provider.load().then(() => {
    home?.refresh();
    paintIcons(root);
  });

  const offLocale = onLocaleChange(() => {
    // Rebuild whatever page is showing; ids come from the live instance.
    if (home) {
      home.refresh();
    } else if (module) {
      showModule(module.categoryId, module.moduleId);
    } else if (category) {
      showCategory(category.categoryId);
    }
    paintIcons(root);
  });

  return {
    destroy(): void {
      offLocale();
      root.remove();
    },
  };
}
