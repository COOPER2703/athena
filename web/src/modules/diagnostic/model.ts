/**
 * Instantané du Diagnostic : normalisation et libellés.
 *
 * Module **pur** : aucun DOM, aucun `fetch`. Le contrat JSON reste en
 * `snake_case` côté serveur ; tout ce qui s'affiche est traduit ici. Un champ
 * absent ou mal typé retombe sur une valeur sûre — l'instantané s'étend sans
 * casser une Console plus ancienne (ADR-0016).
 */

import type { BadgeTone } from "../../shared/ui/Badge";

/** Client branché au transport, tel que l'instantané le décrit. */
export interface ClientInfo {
  clientId: string;
  clientName: string;
  platform: string;
}

/** Session du noyau, telle que l'instantané la décrit. */
export interface SessionStatus {
  state: string;
  sessionId: string;
  connectionId: string;
  activeClientId: string;
}

/** Plugin tel que le registre l'expose (nom, état, Tools attribués). */
export interface PluginInfo {
  name: string;
  state: string;
  tools: string[];
}

/** Configuration non sensible publiée par la racine de composition. */
export type ConsoleConfig = Record<string, string>;

/** Surfaces d'exécution dont l'ouverture dépend de l'Installation. */
export type SurfaceName = "gemini" | "transport" | "webhook";

export const SURFACE_NAMES: readonly SurfaceName[] = [
  "gemini",
  "transport",
  "webhook",
];

export const SURFACE_LABELS: Readonly<Record<SurfaceName, string>> = {
  gemini: "Gemini",
  transport: "Transport des Clients",
  webhook: "Webhook",
};

/** État d'Installation d'une instance (ADR-0026). */
export interface InstallationInfo {
  /** Un Compte existe : la Console est ouverte à la connexion. */
  accountExists: boolean;
  /** Un Secret Gemini utilisable est enregistré (magasin ou environnement). */
  geminiConfigured: boolean;
  /** Compte créé **et** clé Gemini utilisable : l'Installation est finie. */
  complete: boolean;
  /** La clé maître est perdue : les Secrets sont illisibles. */
  secretsUnreadable: boolean;
  /** Surfaces réellement ouvertes dans ce processus. */
  surfaces: Readonly<Record<SurfaceName, boolean>>;
}

/** Dernière Trace de niveau erreur, sans son payload. */
export interface LastErrorInfo {
  timestamp: number | null;
  source: string;
  kind: string;
  level: string;
}

/** Bilan de la dernière Consolidation exécutée. */
export interface ConsolidationInfo {
  at: number | null;
  factsExtracted: number;
  factsStored: number;
  duplicates: number;
  purged: number;
}

/** Borne du Tampon de traces de la Console (taille occupée / capacité). */
export interface TraceBufferInfo {
  size: number;
  capacity: number;
}

/** Bandeau de santé de l'instance. */
export interface HealthInfo {
  version: string;
  uptimeSeconds: number | null;
  lastConsolidation: ConsolidationInfo | null;
  databases: Readonly<Record<string, number>>;
  lastError: LastErrorInfo | null;
  traceBuffer: TraceBufferInfo | null;
}

/** Noms des bases publiées, dans l'ordre du bandeau. */
export const DATABASE_NAMES = ["memory", "auth", "settings"] as const;

export const DATABASE_LABELS: Readonly<Record<string, string>> = {
  memory: "Mémoire",
  auth: "Comptes",
  settings: "Réglages",
};

/** Instantané complet, sans l'enveloppe versionnée `v`. */
export interface DiagnosticSnapshot {
  session: SessionStatus;
  clients: ClientInfo[];
  plugins: PluginInfo[];
  config: ConsoleConfig;
  installation: InstallationInfo;
  restartRequired: boolean;
  health: HealthInfo;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asCount(value: unknown): number {
  const number = asNumber(value);
  return number === null ? 0 : Math.max(0, Math.trunc(number));
}

function asBoolean(value: unknown): boolean {
  return value === true;
}

function asTextList(value: unknown): string[] {
  return asList(value).filter((item): item is string => typeof item === "string");
}

/** État vide : ce que la page affiche avant le premier instantané. */
export function emptySnapshot(): DiagnosticSnapshot {
  return {
    session: {
      state: "",
      sessionId: "",
      connectionId: "",
      activeClientId: "",
    },
    clients: [],
    plugins: [],
    config: {},
    installation: {
      accountExists: false,
      geminiConfigured: false,
      complete: false,
      secretsUnreadable: false,
      surfaces: { gemini: false, transport: false, webhook: false },
    },
    restartRequired: false,
    health: {
      version: "",
      uptimeSeconds: null,
      lastConsolidation: null,
      databases: { memory: 0, auth: 0, settings: 0 },
      lastError: null,
      traceBuffer: null,
    },
  };
}

/**
 * Normalise un instantané reçu par l'API ou par le flux, sans jamais lever.
 *
 * Les sections ajoutées au fil des tickets (installation, santé) sont lues de
 * façon tolérante : une Console déployée avant le serveur affiche « inconnu »
 * plutôt qu'un écran cassé.
 */
export function normalizeSnapshot(raw: unknown): DiagnosticSnapshot {
  const root = isRecord(raw) ? raw : {};
  const session = isRecord(root.session) ? root.session : {};
  const config = isRecord(root.config) ? root.config : {};

  return {
    session: {
      state: asText(session.state),
      sessionId: asText(session.session_id),
      connectionId: asText(session.connection_id),
      activeClientId: asText(session.active_client_id),
    },
    clients: asList(root.clients)
      .filter(isRecord)
      .map((client) => ({
        clientId: asText(client.client_id),
        clientName: asText(client.client_name),
        platform: asText(client.platform),
      })),
    plugins: asList(root.plugins)
      .filter(isRecord)
      .map((plugin) => ({
        name: asText(plugin.name),
        state: asText(plugin.state),
        tools: asTextList(plugin.tools),
      })),
    config: Object.fromEntries(
      Object.entries(config).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string",
      ),
    ),
    installation: normalizeInstallation(root.installation),
    restartRequired: asBoolean(root.restart_required),
    health: normalizeHealth(root.health),
  };
}

function normalizeInstallation(raw: unknown): InstallationInfo {
  const record = isRecord(raw) ? raw : {};
  const surfaces = isRecord(record.surfaces) ? record.surfaces : {};
  return {
    accountExists: asBoolean(record.account_exists),
    geminiConfigured: asBoolean(record.gemini_configured),
    complete: asBoolean(record.complete),
    secretsUnreadable: asBoolean(record.secrets_unreadable),
    surfaces: {
      gemini: asBoolean(surfaces.gemini),
      transport: asBoolean(surfaces.transport),
      webhook: asBoolean(surfaces.webhook),
    },
  };
}

function normalizeHealth(raw: unknown): HealthInfo {
  const record = isRecord(raw) ? raw : {};
  const databases = isRecord(record.databases) ? record.databases : {};
  return {
    version: asText(record.version),
    uptimeSeconds: nonNegative(record.uptime_seconds),
    lastConsolidation: normalizeConsolidation(record.last_consolidation),
    databases: Object.fromEntries(
      DATABASE_NAMES.map((name) => [name, asCount(databases[name])]),
    ),
    lastError: normalizeLastError(record.last_error),
    traceBuffer: normalizeTraceBuffer(record.trace_buffer),
  };
}

function nonNegative(value: unknown): number | null {
  const number = asNumber(value);
  return number === null ? null : Math.max(0, number);
}

function normalizeConsolidation(raw: unknown): ConsolidationInfo | null {
  if (!isRecord(raw)) {
    return null;
  }
  return {
    at: asNumber(raw.at),
    factsExtracted: asCount(raw.facts_extracted),
    factsStored: asCount(raw.facts_stored),
    duplicates: asCount(raw.duplicates),
    purged: asCount(raw.purged),
  };
}

function normalizeLastError(raw: unknown): LastErrorInfo | null {
  if (!isRecord(raw)) {
    return null;
  }
  return {
    timestamp: asNumber(raw.timestamp),
    source: asText(raw.source),
    kind: asText(raw.kind),
    level: asText(raw.level),
  };
}

function normalizeTraceBuffer(raw: unknown): TraceBufferInfo | null {
  if (!isRecord(raw)) {
    return null;
  }
  return {
    size: asCount(raw.size),
    capacity: asCount(raw.capacity),
  };
}

/** Libellés français des états de Session du noyau. */
const SESSION_STATE_LABELS: Readonly<Record<string, string>> = {
  Idle: "Inactive",
  LiveConnecting: "Connexion Live",
  LiveActive: "Live actif",
  SessionVisible: "Session en cours",
  Closing: "Fermeture",
};

/** Libellé d'un état de Session ; l'état brut passe s'il est inconnu. */
export function formatSessionState(state: string): string {
  if (!state) {
    return "Inconnu";
  }
  return SESSION_STATE_LABELS[state] ?? state;
}

/** Tonalité d'un état de Session : actif, transitoire ou au repos. */
export function sessionStateTone(state: string): BadgeTone {
  if (state === "SessionVisible" || state === "LiveActive") {
    return "accent";
  }
  if (state === "LiveConnecting" || state === "Closing") {
    return "warning";
  }
  return "neutral";
}

/** Identifiant raccourci pour rester lisible quand le nom manque. */
export function shortClientId(clientId: string, maxLength = 8): string {
  if (clientId.length <= maxLength) {
    return clientId;
  }
  return `${clientId.slice(0, maxLength)}…`;
}

/** Nom affiché d'un Client : son nom, son identifiant court à défaut. */
export function clientDisplayName(client: ClientInfo): string {
  return client.clientName || shortClientId(client.clientId);
}

/** Vrai si ce Client est le Client propriétaire de la Session visible. */
export function isActiveClient(
  clientId: string,
  activeClientId: string,
): boolean {
  return clientId !== "" && clientId === activeClientId;
}

/** Nom du Client propriétaire, son identifiant court à défaut, « Aucun » sinon. */
export function activeClientLabel(
  activeClientId: string,
  clients: readonly ClientInfo[],
): string {
  if (!activeClientId) {
    return "Aucun";
  }
  const client = clients.find((item) => item.clientId === activeClientId);
  return client?.clientName
    ? client.clientName
    : shortClientId(activeClientId);
}

/** Résumé prêt à afficher dans le panneau Session. */
export interface SessionSummary {
  stateLabel: string;
  sessionId: string;
  activeClient: string;
}

export function sessionSummary(
  session: SessionStatus,
  clients: readonly ClientInfo[],
): SessionSummary {
  return {
    stateLabel: formatSessionState(session.state),
    sessionId: session.sessionId || "—",
    activeClient: activeClientLabel(session.activeClientId, clients),
  };
}

// États de Plugin : une seule source, partagée avec la zone Plugins (C7).
export {
  formatPluginState,
  isPluginState,
  pluginStateTone,
  type PluginState,
} from "../../shared/pluginState";

/**
 * Ce qui empêche l'Instance d'être pleinement opérationnelle.
 *
 * Sert au bandeau du Diagnostic : l'Installation incomplète reste **lisible**
 * (version, santé, Traces) au lieu d'un écran vide.
 */
export interface InstallationIssue {
  tone: "warning" | "danger";
  title: string;
  body: string;
}

export function installationIssues(
  snapshot: DiagnosticSnapshot,
): InstallationIssue[] {
  const issues: InstallationIssue[] = [];
  const { installation } = snapshot;

  if (installation.secretsUnreadable) {
    issues.push({
      tone: "danger",
      title: "Secrets illisibles",
      body: "La clé maître est perdue : les Secrets enregistrés ne peuvent plus être déchiffrés. Re-saisissez-les depuis les Réglages ; rien n'a été réinitialisé en silence.",
    });
  }
  if (!installation.accountExists) {
    issues.push({
      tone: "warning",
      title: "Aucun Compte",
      body: "L'instance n'est pas installée : créez l'unique Compte pour ouvrir la Console.",
    });
  }
  if (installation.accountExists && !installation.geminiConfigured) {
    issues.push({
      tone: "warning",
      title: "Clé Gemini manquante",
      body: "Athena ne peut ni parler ni consolider sa mémoire tant qu'aucune clé Gemini utilisable n'est enregistrée.",
    });
  }
  if (snapshot.restartRequired) {
    issues.push({
      tone: "warning",
      title: "Redémarrage requis",
      body: "Une clé Gemini est enregistrée mais ce processus a démarré sans elle : les surfaces d'exécution restent fermées jusqu'au prochain démarrage.",
    });
  }
  return issues;
}
