/**
 * Flux de traces de la Console, sans navigateur.
 *
 * Ces fonctions pures traduisent les trames du WebSocket `/api/stream` en état
 * d'affichage et en libellés français. La reconnexion remplace le backlog et le
 * log : elle ne concatène jamais deux historiques.
 */

/** Une Trace telle que le contrat JSON la sérialise. */
export interface TraceEntry {
  timestamp: number;
  source: string;
  kind: string;
  level: string;
  payload: Record<string, unknown>;
}

/** Trame d'ouverture : instantané de l'état et historique récent. */
export interface HelloFrame {
  v: number;
  type: "hello";
  data: {
    snapshot: Record<string, unknown>;
    backlog: TraceEntry[];
  };
}

/** Trame vivante du flux. */
export interface TraceFrame {
  v: number;
  type: "trace";
  data: TraceEntry;
}

export type StreamFrame = HelloFrame | TraceFrame;

/** Kind réservé du marqueur unique de troncature côté serveur. */
export const TRUNCATION_KIND = "TraceTruncated";

/** État affiché par le panneau de traces. */
export interface FeedState {
  entries: TraceEntry[];
  /** Nombre cumulé d'entrées perdues signalées par le marqueur. */
  truncated: number;
}

/** Nombre de traces gardées côté navigateur (le serveur borne déjà son tampon). */
export const MAX_FEED_ENTRIES = 500;

/** État initial, avant la première trame `hello`. */
export function emptyFeed(): FeedState {
  return { entries: [], truncated: 0 };
}

/** Vrai pour le marqueur de troncature réservé. */
export function isTruncation(entry: TraceEntry): boolean {
  return entry.kind === TRUNCATION_KIND;
}

/**
 * Remplace l'état affiché par le backlog d'une nouvelle connexion.
 *
 * Une reconnexion repart de l'instantané du serveur : concaténer l'ancien log
 * donnerait des doublons et un historique qui n'existe plus. Le marqueur de
 * troncature est remis à zéro pour la même raison.
 */
export function resetOnHello(_state: FeedState, frame: HelloFrame): FeedState {
  return {
    entries: frame.data.backlog.slice(),
    truncated: 0,
  };
}

/**
 * Applique une trace vivante.
 *
 * Le marqueur de troncature n'entre pas dans le log : il alimente un compteur
 * affiché à part. La borne navigateur évite une croissance sans fin.
 */
export function applyTrace(
  state: FeedState,
  entry: TraceEntry,
  capacity = MAX_FEED_ENTRIES,
): FeedState {
  if (isTruncation(entry)) {
    const dropped = Number(entry.payload.dropped ?? 0);
    return {
      entries: state.entries,
      truncated: state.truncated + (Number.isFinite(dropped) ? dropped : 0),
    };
  }

  const entries = [...state.entries, entry];
  if (entries.length > capacity) {
    entries.splice(0, entries.length - capacity);
  }
  return { entries, truncated: state.truncated };
}

/** Horodatage en heure locale, avec millisecondes pour les logs denses. */
export function formatTimestamp(seconds: number): string {
  const date = new Date(seconds * 1000);
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}.${pad(date.getMilliseconds(), 3)}`;
}

/** Payload lisible : JSON indenté, ou chaîne vide s'il n'y a rien à montrer. */
export function formatPayload(payload: Record<string, unknown>): string {
  if (Object.keys(payload).length === 0) {
    return "";
  }
  return JSON.stringify(payload, null, 2);
}

/** Options du backoff exponentiel de reconnexion. */
export interface BackoffOptions {
  baseMs: number;
  maxMs: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = { baseMs: 500, maxMs: 30_000 };

/**
 * Délai avant la tentative suivante : double à chaque échec, plafonné.
 *
 * Fonction pure et déterministe (pas de jitter) : c'est la machine de
 * reconnexion qu'on teste, pas le hasard.
 */
export function backoffDelay(
  attempt: number,
  options: BackoffOptions = DEFAULT_BACKOFF,
): number {
  const step = Math.max(0, Math.floor(attempt));
  return Math.min(options.baseMs * 2 ** step, options.maxMs);
}

/** État de la connexion WebSocket, tel que l'en-tête l'affiche. */
export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";

/** Libellé français de l'état de connexion. */
export function connectionLabel(status: ConnectionStatus): string {
  switch (status) {
    case "connected":
      return "Connecté";
    case "connecting":
      return "Connexion…";
    case "reconnecting":
      return "Reconnexion…";
    case "disconnected":
      return "Déconnecté";
  }
}
