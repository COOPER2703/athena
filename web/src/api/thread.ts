/**
 * Fil de la Conversation : lecture paginée du Journal (ADR-0024).
 *
 * Le fil n'est pas un état du noyau : c'est une lecture, marquée par l'origine
 * de chaque tour (clavier, micro, Annonce). La pagination remonte vers le passé
 * avec le curseur du serveur (`next_before`, le `rowid` du Journal).
 */

import { requestJson, type Fetcher } from "./client";

/** Taille de page demandée par défaut, comme le serveur. */
export const THREAD_PAGE_SIZE = 50;

/** Origine d'un tour ; `unknown` couvre une origine ajoutée plus tard. */
export type ThreadOrigin = "text" | "voice" | "event" | "unknown";

const ORIGINS: readonly ThreadOrigin[] = ["text", "voice", "event"];

/** Une entrée du fil, prête à afficher. */
export interface ThreadEntry {
  /** Curseur stable de pagination (le `rowid` du Journal). */
  id: number;
  role: string;
  origin: ThreadOrigin;
  text: string;
  /** Horodatage ISO 8601 UTC, mis en forme par l'écran. */
  createdAt: string;
}

/** Une page du fil, du plus ancien au plus récent. */
export interface ThreadPage {
  entries: ThreadEntry[];
  /** Curseur de la page suivante (plus ancienne), ou `null` au début du fil. */
  nextBefore: number | null;
  /** Fenêtre de rétention du Journal, en jours. */
  retentionDays: number;
}

/** Paramètres d'une lecture : le curseur est absent pour la page récente. */
export interface ThreadQuery {
  before?: number | null;
  limit?: number;
}

/** Lit une page du fil. */
export async function fetchThread(
  query: ThreadQuery = {},
  fetcher?: Fetcher,
): Promise<ThreadPage> {
  const params = new URLSearchParams({
    limit: String(query.limit ?? THREAD_PAGE_SIZE),
  });
  if (typeof query.before === "number") {
    params.set("before", String(query.before));
  }
  return normalizeThreadPage(
    await requestJson(`/api/thread?${params.toString()}`, {}, fetcher),
  );
}

/**
 * Normalise une page reçue du serveur, sans jamais lever.
 *
 * Une entrée sans identifiant numérique est ignorée : elle ne serait pas
 * paginable et casserait la clé de rendu. Le reste retombe sur des valeurs
 * sûres, pour qu'une Console plus ancienne lise un serveur plus récent.
 */
export function normalizeThreadPage(raw: unknown): ThreadPage {
  const root = asRecord(raw);
  const entries: ThreadEntry[] = [];
  for (const item of Array.isArray(root.entries) ? root.entries : []) {
    const entry = normalizeEntry(item);
    if (entry !== null) {
      entries.push(entry);
    }
  }
  return {
    entries,
    nextBefore: positiveInteger(root.next_before),
    retentionDays: count(root.retention_days),
  };
}

/**
 * Met les pages du cache (de la plus récente à la plus ancienne) bout à bout,
 * du plus ancien au plus récent, sans jamais répéter une entrée.
 */
export function flattenThread(
  pages: readonly ThreadPage[] | undefined,
): ThreadEntry[] {
  if (pages === undefined) {
    return [];
  }
  const seen = new Set<number>();
  const entries: ThreadEntry[] = [];
  for (let index = pages.length - 1; index >= 0; index -= 1) {
    const page = pages[index];
    if (page === undefined) {
      continue;
    }
    for (const entry of page.entries) {
      if (seen.has(entry.id)) {
        continue;
      }
      seen.add(entry.id);
      entries.push(entry);
    }
  }
  return entries;
}

function normalizeEntry(raw: unknown): ThreadEntry | null {
  const record = asRecord(raw);
  const id = positiveInteger(record.id);
  if (id === null) {
    return null;
  }
  return {
    id,
    role: text(record.role),
    origin: origin(record.origin),
    text: text(record.text),
    createdAt: text(record.created_at),
  };
}

function origin(value: unknown): ThreadOrigin {
  return typeof value === "string" &&
    (ORIGINS as readonly string[]).includes(value)
    ? (value as ThreadOrigin)
    : "unknown";
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : null;
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.trunc(value)
    : 0;
}
