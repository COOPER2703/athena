/**
 * Accès à la zone Plugins de l'API : liste, activation, configuration, secrets.
 *
 * Le serveur ne rend **jamais** de valeur de Secret : `PluginSecretField`
 * porte le nom de la variable d'environnement attendue, `configured` et la
 * date de mise à jour. Toute réponse est normalisée défensivement : un champ
 * absent ou mal typé retombe sur une valeur sûre plutôt que de casser l'écran.
 */

import { jsonBody, requestJson, type Fetcher } from "./client";

/** États de chargement d'un Plugin, tels que le chargeur les décrit. */
export type PluginState = "activated" | "disabled" | "missing" | "failed";

const PLUGIN_STATES: readonly PluginState[] = [
  "activated",
  "disabled",
  "missing",
  "failed",
];

/** Vrai pour un état connu du serveur. */
export function isPluginState(value: unknown): value is PluginState {
  return (
    typeof value === "string" &&
    (PLUGIN_STATES as readonly string[]).includes(value)
  );
}

/** Champ de configuration déclaré par le manifeste, avec sa valeur actuelle. */
export interface PluginConfigField {
  key: string;
  type: "string" | "url";
  required: boolean;
  value: string;
}

/** Secret déclaré par le manifeste : en écriture seule, jamais relu. */
export interface PluginSecretField {
  key: string;
  required: boolean;
  /** Nom de la variable d'environnement attendue (`ATHENA_PLUGIN_…`). */
  variable: string;
  configured: boolean;
  updatedAt: number | null;
}

/** Un Plugin installé, tel que la Console le montre. */
export interface PluginEntry {
  name: string;
  enabled: boolean;
  state: PluginState;
  version: string;
  platform: string;
  description: string;
  tools: readonly string[];
  instruction: string;
  config: readonly PluginConfigField[];
  secrets: readonly PluginSecretField[];
  /** Vrai quand le fichier d'état diffère de ce qui a été chargé. */
  pendingRestart: boolean;
  /** Raison lisible d'un manifeste illisible ou d'un chargement en échec. */
  error: string;
}

/** Réponse normalisée de `GET /api/plugins`. */
export interface PluginsPayload {
  plugins: readonly PluginEntry[];
  restartRequired: boolean;
}

/** Modification envoyée par le formulaire : l'un, l'autre, ou les deux. */
export interface PluginUpdate {
  enabled?: boolean;
  config?: Readonly<Record<string, string>>;
}

/**
 * Normalise une liste de Plugins.
 *
 * Un Plugin sans nom est ignoré (il ne serait pas actionnable) ; tout le reste
 * est ramené à une forme sûre, y compris un état inconnu, traité comme
 * « désactivé » faute de mieux.
 */
export function normalizePlugins(body: unknown): PluginsPayload {
  const record = asRecord(body);
  const rawPlugins = Array.isArray(record.plugins) ? record.plugins : [];
  const plugins: PluginEntry[] = [];
  for (const raw of rawPlugins) {
    const plugin = normalizePlugin(raw);
    if (plugin !== null) {
      plugins.push(plugin);
    }
  }
  return {
    plugins,
    restartRequired: record.restart_required === true,
  };
}

function normalizePlugin(raw: unknown): PluginEntry | null {
  const record = asRecord(raw);
  const name = typeof record.name === "string" ? record.name.trim() : "";
  if (name === "") {
    return null;
  }
  return {
    name,
    enabled: record.enabled === true,
    state: isPluginState(record.state) ? record.state : "disabled",
    version: text(record.version),
    platform: text(record.platform),
    description: text(record.description),
    tools: texts(record.tools),
    instruction: text(record.instruction),
    config: fields(record.config).map(normalizeConfigField),
    secrets: fields(record.secrets).map(normalizeSecretField),
    pendingRestart: record.pending_restart === true,
    error: text(record.error),
  };
}

function normalizeConfigField(raw: unknown): PluginConfigField {
  const record = asRecord(raw);
  return {
    key: text(record.key),
    type: record.type === "url" ? "url" : "string",
    required: record.required === true,
    value: text(record.value),
  };
}

function normalizeSecretField(raw: unknown): PluginSecretField {
  const record = asRecord(raw);
  const updatedAt = record.updated_at;
  return {
    key: text(record.key),
    required: record.required === true,
    variable: text(record.variable),
    configured: record.configured === true,
    updatedAt:
      typeof updatedAt === "number" && Number.isFinite(updatedAt)
        ? updatedAt
        : null,
  };
}

/** Lit l'état, l'activation et la configuration de tous les Plugins. */
export async function fetchPlugins(
  fetcher?: Fetcher,
): Promise<PluginsPayload> {
  return normalizePlugins(await requestJson("/api/plugins", {}, fetcher));
}

/** Persiste l'activation et/ou la configuration ; l'effet est au redémarrage. */
export async function updatePlugin(
  name: string,
  update: PluginUpdate,
  fetcher?: Fetcher,
): Promise<{ plugin: PluginEntry | null; restartRequired: boolean }> {
  const body = await requestJson(
    `/api/plugins/${encodeURIComponent(name)}`,
    { method: "PUT", ...jsonBody(update) },
    fetcher,
  );
  const record = asRecord(body);
  return {
    plugin: normalizePlugin(record.plugin),
    restartRequired: record.restart_required === true,
  };
}

/** Enregistre un Secret ; la valeur ne revient jamais. */
export async function setPluginSecret(
  name: string,
  key: string,
  value: string,
  fetcher?: Fetcher,
): Promise<PluginSecretField> {
  const body = await requestJson(
    secretPath(name, key),
    { method: "PUT", ...jsonBody({ value }) },
    fetcher,
  );
  return normalizeSecretField(asRecord(body).secret);
}

/** Efface un Secret ; l'absence n'est pas une erreur. */
export async function deletePluginSecret(
  name: string,
  key: string,
  fetcher?: Fetcher,
): Promise<PluginSecretField> {
  const body = await requestJson(secretPath(name, key), { method: "DELETE" }, fetcher);
  return normalizeSecretField(asRecord(body).secret);
}

function secretPath(name: string, key: string): string {
  return `/api/plugins/${encodeURIComponent(name)}/secrets/${encodeURIComponent(key)}`;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function fields(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function texts(value: unknown): string[] {
  return fields(value).filter((item): item is string => typeof item === "string");
}
