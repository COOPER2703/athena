/**
 * Réglages de la Console : lecture, écriture et état du redémarrage.
 *
 * Le serveur rend la **valeur effective** de chaque champ, sa source, les
 * changements en attente et les Surfaces dont l'adresse peut changer. Toute
 * réponse est normalisée défensivement : un champ absent ou mal typé retombe
 * sur une valeur sûre, et un réglage inconnu n'est jamais inventé.
 */

import { jsonBody, requestJson, type Fetcher } from "./client";
import type { SecretStatus } from "./secrets";

/** Provenance d'une valeur effective, telle que l'interface la montre. */
export type SettingSource = "default" | "store" | "env";

/** Forme d'un champ, qui borne le contrôle affiché et la validation locale. */
export type SettingKind = "string" | "int" | "float" | "bool" | "hours";

/** Un réglage tel que `GET /api/settings` le décrit. */
export interface SettingField {
  key: string;
  /** Variable d'environnement attendue, affichée quand elle masque le champ. */
  env: string;
  kind: SettingKind;
  /** Valeur **active**, celle que le processus a chargée. */
  value: string;
  defaultValue: string;
  source: SettingSource;
  editable: boolean;
  /** Vrai quand l'interface ne doit pas proposer d'écriture. */
  readOnly: boolean;
  pending: boolean;
  /** Valeur stockée en attente ; `null` signifie « retour au défaut ». */
  pendingValue: string | null;
  minimum: number | null;
  maximum: number | null;
  choices: readonly string[];
}

/** Surface réseau dont une adresse change au redémarrage. */
export interface SettingsSurface {
  name: string;
  /** Clé du réglage d'hôte (`console_host`, `webhook_host`, `server_host`). */
  hostKey: string;
  /** Clé du réglage de port. */
  portKey: string;
  /** `http`, `https`, `ws` ou `wss`. */
  scheme: string;
  /** URL active, telle que le serveur la sert aujourd'hui. */
  url: string;
}

/** Ce que le serveur dit du redémarrage nécessaire. */
export interface RestartState {
  required: boolean;
  /** Vrai quand le serveur sort seul et s'appuie sur son superviseur. */
  automatic: boolean;
  /** Vrai quand le dernier `PUT` a demandé ce redémarrage. */
  requested: boolean;
  /** Commande exacte à exécuter quand le redémarrage est manuel. */
  command: string;
}

/** Réponse normalisée de `GET` et `PUT /api/settings`. */
export interface SettingsPayload {
  fields: readonly SettingField[];
  surfaces: readonly SettingsSurface[];
  restart: RestartState;
  secrets: readonly SecretStatus[];
  secretsUnreadable: boolean;
}

const SOURCES: readonly SettingSource[] = ["default", "store", "env"];
const KINDS: readonly SettingKind[] = ["string", "int", "float", "bool", "hours"];

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isSource(value: unknown): value is SettingSource {
  return typeof value === "string" && (SOURCES as readonly string[]).includes(value);
}

function isKind(value: unknown): value is SettingKind {
  return typeof value === "string" && (KINDS as readonly string[]).includes(value);
}

function readChoices(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string");
}

/** Normalise un champ ; `null` quand sa clé manque. */
function normalizeField(
  key: string,
  raw: unknown,
): SettingField | null {
  const name = key.trim();
  if (name === "") {
    return null;
  }
  const record = asRecord(raw);
  const source: SettingSource = isSource(record.source)
    ? record.source
    : "default";
  const editable = record.editable !== false;
  const pending = record.pending === true;
  return {
    key: name,
    env: text(record.env),
    kind: isKind(record.kind) ? record.kind : "string",
    value: text(record.value),
    defaultValue: text(record.default),
    source,
    editable,
    // L'environnement masque toujours le champ, quelle que soit la réponse.
    readOnly: record.read_only === true || !editable || source === "env",
    pending,
    pendingValue:
      pending && typeof record.pending_value === "string"
        ? record.pending_value
        : null,
    minimum: readNumber(record.minimum),
    maximum: readNumber(record.maximum),
    choices: readChoices(record.choices),
  };
}

function normalizeFields(raw: unknown): SettingField[] {
  const record = asRecord(raw);
  const fields: SettingField[] = [];
  const seen = new Set<string>();
  for (const [key, value] of Object.entries(record)) {
    const field = normalizeField(key, value);
    if (field === null || seen.has(field.key)) {
      continue;
    }
    seen.add(field.key);
    fields.push(field);
  }
  return fields;
}

function normalizeSurface(raw: unknown): SettingsSurface | null {
  const record = asRecord(raw);
  const name = text(record.name).trim();
  const hostKey = text(record.host).trim();
  const portKey = text(record.port).trim();
  if (name === "" || hostKey === "" || portKey === "") {
    return null;
  }
  return {
    name,
    hostKey,
    portKey,
    scheme: text(record.scheme) || "http",
    url: text(record.url),
  };
}

function normalizeSurfaces(raw: unknown): SettingsSurface[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.flatMap((entry) => {
    const surface = normalizeSurface(entry);
    return surface === null ? [] : [surface];
  });
}

function normalizeRestart(raw: unknown, required: boolean): RestartState {
  const record = asRecord(raw);
  return {
    required: record.required === true || required,
    automatic: record.automatic === true,
    requested: record.requested === true,
    command: text(record.command),
  };
}

function normalizeSecret(raw: unknown): SecretStatus | null {
  const record = asRecord(raw);
  const name = text(record.name).trim();
  if (name === "") {
    return null;
  }
  const updatedAt = record.updated_at;
  return {
    name,
    configured: record.configured === true,
    updatedAt:
      typeof updatedAt === "number" && Number.isFinite(updatedAt)
        ? updatedAt
        : null,
  };
}

function normalizeSecrets(raw: unknown): SecretStatus[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.flatMap((entry) => {
    const secret = normalizeSecret(entry);
    return secret === null ? [] : [secret];
  });
}

/** Normalise la réponse complète des Réglages. */
export function normalizeSettings(body: unknown): SettingsPayload {
  const record = asRecord(body);
  return {
    fields: normalizeFields(record.fields),
    surfaces: normalizeSurfaces(record.surfaces),
    restart: normalizeRestart(record.restart, record.restart_required === true),
    secrets: normalizeSecrets(record.secrets),
    secretsUnreadable: record.secrets_unreadable === true,
  };
}

/** Lit la valeur effective, les sources et les changements en attente. */
export async function fetchSettings(
  fetcher?: Fetcher,
): Promise<SettingsPayload> {
  return normalizeSettings(await requestJson("/api/settings", {}, fetcher));
}

/**
 * Écrit l'objet **complet** des champs écrivables.
 *
 * `confirmUrls` porte les URL des Surfaces déplacées : le serveur refuse un
 * changement d'hôte ou de port tant qu'elles ne sont pas confirmées.
 */
export async function saveSettings(
  values: Readonly<Record<string, string>>,
  confirmUrls?: readonly string[],
  fetcher?: Fetcher,
): Promise<SettingsPayload> {
  const payload: Record<string, unknown> = { values: { ...values } };
  if (confirmUrls !== undefined && confirmUrls.length > 0) {
    payload.confirm_url = [...confirmUrls];
  }
  const body = await requestJson(
    "/api/settings",
    { method: "PUT", ...jsonBody(payload) },
    fetcher,
  );
  return normalizeSettings(body);
}
