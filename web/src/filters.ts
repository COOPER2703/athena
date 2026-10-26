/**
 * Filtrage et pause du flux de traces, sans navigateur.
 *
 * Ces fonctions sont pures : elles prennent les traces reçues et l'état des
 * contrôles, et rendent la liste à afficher. Le flux continue d'arriver pendant
 * une pause ; seule la vue est gelée.
 */

import type { TraceEntry } from "./trace";

/** État des contrôles de filtrage ; une chaîne vide signifie « tout ». */
export interface TraceFilters {
  level: string;
  source: string;
  text: string;
}

/** Filtres neutres : toutes les traces, tous les niveaux et toutes les sources. */
export const EMPTY_FILTERS: TraceFilters = {
  level: "",
  source: "",
  text: "",
};

/**
 * Minuscules sans accents, pour une recherche simple et tolérante.
 *
 * La décomposition NFD sépare les diacritiques, que l'on retire ensuite : une
 * recherche « deconnexion » retrouve « Déconnexion » sans table de conversion.
 */
export function normalizeText(value: string): string {
  return value
    .toLocaleLowerCase("fr")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "");
}

/** Texte sérialisé d'un payload, sans jamais faire échouer l'affichage. */
function serializedPayload(payload: Record<string, unknown>): string {
  try {
    return JSON.stringify(payload);
  } catch {
    return "";
  }
}

/** Vrai si la trace passe tous les filtres actifs. */
export function matchesFilters(entry: TraceEntry, filters: TraceFilters): boolean {
  if (filters.level && entry.level !== filters.level) {
    return false;
  }
  if (filters.source && entry.source !== filters.source) {
    return false;
  }

  const needle = normalizeText(filters.text.trim());
  if (!needle) {
    return true;
  }
  const haystack = normalizeText(
    `${entry.source} ${entry.kind} ${serializedPayload(entry.payload)}`,
  );
  return haystack.includes(needle);
}

/** Applique les trois filtres à une liste de traces. */
export function filterEntries(
  entries: readonly TraceEntry[],
  filters: TraceFilters,
): TraceEntry[] {
  return entries.filter((entry) => matchesFilters(entry, filters));
}

/** Niveaux présents dans les traces reçues, triés et dédupliqués. */
export function availableLevels(entries: readonly TraceEntry[]): string[] {
  return distinct(entries.map((entry) => entry.level));
}

/** Sources présentes dans les traces reçues, triées et dédupliquées. */
export function availableSources(entries: readonly TraceEntry[]): string[] {
  return distinct(entries.map((entry) => entry.source));
}

function distinct(values: readonly string[]): string[] {
  return Array.from(new Set(values.filter(Boolean))).sort((a, b) =>
    a.localeCompare(b, "fr"),
  );
}

/** Vue du flux : vivante, ou gelée sur les traces présentes à la pause. */
export interface FeedView {
  paused: boolean;
  /** Copie des traces au moment de la pause ; vide en mode vivant. */
  snapshot: readonly TraceEntry[];
}

/** Vue vivante : l'affichage suit le flux. */
export const LIVE_VIEW: FeedView = { paused: false, snapshot: [] };

/** Gèle la vue sur les traces actuellement présentes. */
export function pauseView(entries: readonly TraceEntry[]): FeedView {
  return { paused: true, snapshot: entries.slice() };
}

/** Reprend le flux : la vue repart de toutes les traces, y compris récentes. */
export function resumeView(): FeedView {
  return LIVE_VIEW;
}

/**
 * Traces à afficher.
 *
 * En pause, la source est l'instantané pris à la pause : les nouvelles traces
 * ne s'affichent pas, mais restent dans l'état du flux. Les filtres continuent
 * de s'appliquer à cet instantané, pour rester utiles pendant la lecture.
 */
export function displayedEntries(
  entries: readonly TraceEntry[],
  filters: TraceFilters,
  view: FeedView,
): TraceEntry[] {
  return filterEntries(view.paused ? view.snapshot : entries, filters);
}
