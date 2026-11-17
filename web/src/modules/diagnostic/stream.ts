/**
 * Flux d'observation du Diagnostic, sans navigateur.
 *
 * Ces fonctions pures traduisent les trames du WebSocket `/api/stream` en état
 * d'affichage : instantané, backlog et Traces vivantes. La reconnexion remplace
 * le backlog (`hello`) au lieu de le concaténer ; une trame malformée est
 * ignorée, jamais interprétée. Le flux ne sert qu'à observer : aucun Tour ne
 * passe par ce socket.
 */

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

/** Libellé français d'un niveau de Trace. */
export function formatTraceLevel(level: string): string {
  switch (level) {
    case "debug":
      return "Débogage";
    case "info":
      return "Info";
    case "warning":
      return "Avertissement";
    case "error":
      return "Erreur";
    case "critical":
      return "Critique";
    default:
      return level || "Inconnu";
  }
}

/** Tonalité d'un niveau de Trace ; un niveau inconnu reste neutre. */
export function traceLevelTone(level: string): "neutral" | "warning" | "danger" {
  if (level === "error" || level === "critical") {
    return "danger";
  }
  return level === "warning" ? "warning" : "neutral";
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
  data: Record<string, unknown>;
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
  /** Numéro du flux : incrémenté à chaque `hello`. */
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
 * Applique une Trace vivante.
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
  return isRecord(value) && value.type === "trace" && isTraceData(value.data);
}

/** Vrai si la valeur a la forme d'une trame `status`. */
export function isStatusFrame(value: unknown): value is StatusFrame {
  return isRecord(value) && value.type === "status" && isRecord(value.data);
}

/** Trame connue du contrat, ou `null` pour une trame inconnue/malformée. */
export function parseStreamFrame(value: unknown): StreamFrame | null {
  if (isHelloFrame(value)) {
    return value;
  }
  if (isTraceFrame(value)) {
    return value;
  }
  if (isStatusFrame(value)) {
    return value;
  }
  return null;
}

/** Horodatage d'une Trace en heure locale, avec millisecondes. */
export function formatTraceTime(seconds: number): string {
  if (!Number.isFinite(seconds)) {
    return "—";
  }
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  const pad = (value: number, width = 2): string =>
    String(value).padStart(width, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}.${pad(date.getMilliseconds(), 3)}`;
}

/** Payload lisible d'une Trace : JSON indenté, ou chaîne vide. */
export function formatTracePayload(payload: Record<string, unknown>): string {
  if (Object.keys(payload).length === 0) {
    return "";
  }
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return "";
  }
}

/** État de la connexion au flux d'observation. */
export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";

/** Libellé français d'un état de connexion. */
export function connectionLabel(status: ConnectionStatus): string {
  switch (status) {
    case "connected":
      return "Flux connecté";
    case "connecting":
      return "Connexion au flux…";
    case "reconnecting":
      return "Reconnexion…";
    case "disconnected":
      return "Hors ligne";
  }
}

/** Tonalité d'un état de connexion : seul « connecté » est un état sain. */
export function connectionTone(
  status: ConnectionStatus,
): "neutral" | "warning" | "accent" {
  if (status === "connected") {
    return "accent";
  }
  return status === "disconnected" ? "warning" : "neutral";
}

/** Options du backoff de reconnexion. */
export interface BackoffOptions {
  baseMs: number;
  maxMs: number;
}

/** Backoff exponentiel par défaut : 500 ms, doublé jusqu'à 30 s. */
export const DEFAULT_BACKOFF: BackoffOptions = { baseMs: 500, maxMs: 30_000 };

/** Délai avant la `failures`-ième reconnexion (0 = première tentative). */
export function backoffDelay(
  failures: number,
  { baseMs, maxMs }: BackoffOptions = DEFAULT_BACKOFF,
): number {
  if (failures <= 0) {
    return 0;
  }
  return Math.min(maxMs, baseMs * 2 ** (failures - 1));
}
