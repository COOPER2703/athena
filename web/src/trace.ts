/**
 * Flux de traces de la Console, sans navigateur.
 *
 * Ces fonctions pures traduisent les trames du WebSocket `/api/stream` en état
 * d'affichage. La reconnexion remplace le backlog et le log : elle ne concatène
 * jamais deux historiques. Les trames sont validées avant d'entrer dans le
 * reducer, pour qu'une trame malformée n'efface ni ne casse la page.
 */

import type { StatusData } from "./status";

export { formatPayload, formatTimestamp } from "./format";
export {
  DEFAULT_BACKOFF,
  backoffDelay,
  connectionLabel,
  type BackoffOptions,
  type ConnectionStatus,
} from "./connection";

/** Une Trace telle que le contrat JSON la sérialise. */
export interface TraceEntry {
  timestamp: number;
  source: string;
  kind: string;
  level: string;
  payload: Record<string, unknown>;
}

/** Niveaux connus du flux, du plus verbeux au plus grave. */
export type TraceLevel = "debug" | "info" | "warning" | "error" | "critical";

/** Niveaux reconnus par l'affichage, dans l'ordre du flux. */
export const TRACE_LEVELS: readonly TraceLevel[] = [
  "debug",
  "info",
  "warning",
  "error",
  "critical",
];

/** Vrai si la valeur est un niveau connu du flux. */
export function isTraceLevel(value: string): value is TraceLevel {
  return (TRACE_LEVELS as readonly string[]).includes(value);
}

/**
 * Classe CSS d'un niveau, bornée aux valeurs connues.
 *
 * Un niveau inconnu (version future du serveur) retombe sur `other` : aucune
 * valeur brute ne peut injecter une classe arbitraire dans la page.
 */
export function traceLevelClass(level: string): string {
  return isTraceLevel(level) ? level : "other";
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

/** Trame différentielle : l'instantané a changé côté serveur. */
export interface StatusFrame {
  v: number;
  type: "status";
  data: StatusData;
}

export type StreamFrame = HelloFrame | TraceFrame | StatusFrame;

/** Kind réservé du marqueur unique de troncature côté serveur. */
export const TRUNCATION_KIND = "TraceTruncated";

/** État affiché par le panneau de traces. */
export interface FeedState {
  entries: TraceEntry[];
  /** Nombre cumulé d'entrées perdues signalées par le marqueur serveur. */
  truncated: number;
  /** Nombre cumulé d'entrées oubliées par la borne du navigateur. */
  browserDropped: number;
  /** Numéro de la connexion : incrémenté à chaque `hello`. */
  generation: number;
}

/** Nombre de traces gardées côté navigateur (le serveur borne déjà son tampon). */
export const MAX_FEED_ENTRIES = 500;

/** État initial, avant la première trame `hello`. */
export function emptyFeed(): FeedState {
  return { entries: [], truncated: 0, browserDropped: 0, generation: 0 };
}

/** Vrai pour le marqueur de troncature réservé. */
export function isTruncation(entry: TraceEntry): boolean {
  return entry.kind === TRUNCATION_KIND;
}

/**
 * Remplace l'état affiché par le backlog d'une nouvelle connexion.
 *
 * Une reconnexion repart de l'instantané du serveur : concaténer l'ancien log
 * donnerait des doublons et un historique qui n'existe plus. Les compteurs de
 * troncature sont remis à zéro et la génération avance, pour que la vue gelée
 * sache qu'un nouveau flux commence.
 */
export function resetOnHello(state: FeedState, frame: HelloFrame): FeedState {
  const backlog = frame.data.backlog;
  const overflow = Math.max(0, backlog.length - MAX_FEED_ENTRIES);
  return {
    entries: overflow > 0 ? backlog.slice(overflow) : backlog.slice(),
    truncated: 0,
    browserDropped: overflow,
    generation: state.generation + 1,
  };
}

/**
 * Applique une trace vivante.
 *
 * Le marqueur de troncature n'entre pas dans le log : il alimente un compteur
 * affiché à part. La borne navigateur évite une croissance sans fin, mais les
 * entrées qu'elle oublie sont comptées : la perte n'est jamais silencieuse.
 */
export function applyTrace(
  state: FeedState,
  entry: TraceEntry,
  capacity = MAX_FEED_ENTRIES,
): FeedState {
  if (isTruncation(entry)) {
    const dropped = Number(entry.payload.dropped ?? 0);
    return {
      ...state,
      truncated: state.truncated + (Number.isFinite(dropped) ? dropped : 0),
    };
  }

  const next = [...state.entries, entry];
  const overflow = Math.max(0, next.length - capacity);
  return {
    ...state,
    entries: overflow > 0 ? next.slice(overflow) : next,
    browserDropped: state.browserDropped + overflow,
  };
}

/** Vrai pour un objet JSON non nul et non tableau. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Vrai si la valeur a la forme d'une Trace du contrat. */
export function isTraceData(value: unknown): value is TraceEntry {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.timestamp === "number" &&
    Number.isFinite(value.timestamp) &&
    typeof value.source === "string" &&
    typeof value.kind === "string" &&
    typeof value.level === "string" &&
    isRecord(value.payload)
  );
}

/** Vrai si la valeur a la forme d'une trame `hello` complète. */
export function isHelloFrame(value: unknown): value is HelloFrame {
  if (!isRecord(value) || value.type !== "hello" || !isRecord(value.data)) {
    return false;
  }
  const { snapshot, backlog } = value.data;
  return (
    isRecord(snapshot) &&
    Array.isArray(backlog) &&
    backlog.every((entry) => isTraceData(entry))
  );
}

/** Vrai si la valeur a la forme d'une trame `trace`. */
export function isTraceFrame(value: unknown): value is TraceFrame {
  return (
    isRecord(value) && value.type === "trace" && isTraceData(value.data)
  );
}

/** Vrai si la valeur a la forme d'une trame `status`. */
export function isStatusFrame(value: unknown): value is StatusFrame {
  return isRecord(value) && value.type === "status" && isRecord(value.data);
}

/** Vrai si la valeur est une trame connue du contrat. */
export function isStreamFrame(value: unknown): value is StreamFrame {
  return isHelloFrame(value) || isTraceFrame(value) || isStatusFrame(value);
}
