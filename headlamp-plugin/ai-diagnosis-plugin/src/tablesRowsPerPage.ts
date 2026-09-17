/**
 * Headlamp's table "rows per page" persistence, re-implemented locally.
 *
 * The plugin build only externalizes whitelisted SDK subpaths
 * (`@kinvolk/headlamp-plugin/lib/...` → `pluginLib.*`); `lib/helpers` is not
 * among them, so importing it yields undefined at runtime. We use the same
 * localStorage key as Headlamp, so the value stays shared with its tables.
 */

const ROWS_PER_PAGE_KEY = 'tables_rows_per_page';

export function getTablesRowsPerPage(fallback: number): number {
  try {
    const raw = window.localStorage.getItem(ROWS_PER_PAGE_KEY);
    const value = raw === null ? NaN : Number(raw);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  } catch {
    return fallback;
  }
}

export function setTablesRowsPerPage(value: number): void {
  try {
    window.localStorage.setItem(ROWS_PER_PAGE_KEY, String(value));
  } catch {
    // Storage unavailable: keep the in-memory choice for this session only.
  }
}
