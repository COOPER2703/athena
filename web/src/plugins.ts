/**
 * Fiche d'un plugin installé : normalisation défensive et regroupement.
 *
 * L'instantané du serveur reste en `snake_case` et peut être partiel (une
 * trame ancienne, un dossier jamais lu, un manifeste cassé). Ce module est pur
 * et sans DOM : il borne chaque champ à une valeur affichable, et regroupe la
 * matière de la fiche (identité, Tools, Instruction, panne) pour que le
 * composant n'ait aucune logique à porter.
 */

/** Identité déclarée par un manifeste, normalisée pour l'affichage. */
export interface PluginDeclarationInfo {
  description: string;
  version: string;
  /** `0` signifie « absente ou inconnue », jamais une version réelle. */
  apiVersion: number;
  platform: string;
  directory: string;
  invalidReason: string | null;
}

/** Plugin tel que l'instantané le décrit, après normalisation. */
export interface PluginInfo {
  name: string;
  state: string;
  tools: string[];
  /** `null` quand rien n'a pu être lu (dossier manquant, entrée partielle). */
  declaration: PluginDeclarationInfo | null;
  /** Raison d'échec expurgée par le serveur, `null` si le plugin n'échoue pas. */
  reason: string | null;
  /** Vrai quand une retente en tâche de fond est attendue. */
  retry: boolean;
  /** Instruction de plugin ; chaîne vide quand il n'en fournit pas. */
  instruction: string;
}

/** Une ligne de la fiche : libellé, valeur, et police monospace éventuelle. */
export interface PluginFact {
  label: string;
  value: string;
  mono?: boolean;
}

/** Fiche groupée d'un plugin, prête à rendre. */
export interface PluginSheet {
  /** Identité déclarée, `[]` quand aucune Déclaration n'a été lue. */
  identity: PluginFact[];
  tools: string[];
  instruction: string;
  failure: string | null;
  invalidReason: string | null;
  retry: boolean;
  loaded: boolean;
  declarationMissing: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function asTextOrNull(value: unknown): string | null {
  const text = asText(value);
  return text === "" ? null : text;
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asApiVersion(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : 0;
}

function normalizeDeclaration(raw: unknown): PluginDeclarationInfo | null {
  if (!isRecord(raw)) {
    return null;
  }
  return {
    description: asText(raw.description),
    version: asText(raw.version),
    apiVersion: asApiVersion(raw.api_version),
    platform: asText(raw.platform),
    directory: asText(raw.directory),
    invalidReason: asTextOrNull(raw.invalid_reason),
  };
}

/** Normalise une entrée plugin ; `null` si ce n'est pas un objet. */
export function normalizePlugin(raw: unknown): PluginInfo | null {
  if (!isRecord(raw)) {
    return null;
  }
  return {
    name: asText(raw.name),
    state: asText(raw.state),
    tools: asList(raw.tools).filter(
      (tool): tool is string => typeof tool === "string",
    ),
    declaration: normalizeDeclaration(raw.declaration),
    reason: asTextOrNull(raw.reason),
    retry: raw.retry === true,
    instruction: asText(raw.instruction),
  };
}

/** Normalise la liste `plugins` d'un instantané, en écartant les non-objets. */
export function normalizePlugins(raw: unknown): PluginInfo[] {
  return asList(raw)
    .map(normalizePlugin)
    .filter((plugin): plugin is PluginInfo => plugin !== null);
}

/**
 * Regroupe la fiche : identité déclarée, Tools, Instruction et panne.
 *
 * Une `api_version` nulle est omise : l'afficher comme « 0 » ferait passer une
 * absence de lecture pour une version réelle. Un dossier sans Déclaration le
 * dit par `declarationMissing`, pour que l'interface n'invente rien.
 */
export function pluginSheet(plugin: PluginInfo): PluginSheet {
  const declaration = plugin.declaration;
  const identity: PluginFact[] = [];
  if (declaration !== null) {
    identity.push({
      label: "Description",
      value: declaration.description || "—",
    });
    identity.push({
      label: "Version",
      value: declaration.version || "—",
      mono: true,
    });
    if (declaration.apiVersion > 0) {
      identity.push({
        label: "api_version",
        value: String(declaration.apiVersion),
        mono: true,
      });
    }
    identity.push({
      label: "Plateforme",
      value: declaration.platform || "—",
    });
    identity.push({
      label: "Dossier",
      value: declaration.directory || "—",
      mono: true,
    });
  }
  return {
    identity,
    tools: plugin.tools,
    instruction: plugin.instruction,
    failure: plugin.reason,
    invalidReason: declaration?.invalidReason ?? null,
    retry: plugin.retry,
    loaded: plugin.state === "activated",
    declarationMissing: declaration === null,
  };
}

/**
 * Phrase d'absence d'Instruction, `null` quand le plugin en fournit une.
 *
 * Un plugin non chargé n'a par définition aucune Instruction : le dire
 * explicitement évite de confondre « rien à lire » et « plugin muet ».
 */
export function instructionNotice(plugin: PluginInfo): string | null {
  if (plugin.instruction !== "") {
    return null;
  }
  return plugin.state === "activated"
    ? "Ce plugin est chargé mais ne fournit aucune Instruction."
    : "Aucune Instruction : ce plugin n'est pas chargé.";
}

/** États connus du Plugin Registry, et leur présentation française. */
export type PluginState =
  | "activated"
  | "failed"
  | "missing"
  | "inactive"
  | "unsupported"
  | "invalid";

const PLUGIN_STATES: Readonly<Record<PluginState, { label: string }>> = {
  activated: { label: "Activé" },
  failed: { label: "En échec" },
  missing: { label: "Manquant" },
  inactive: { label: "Inactif" },
  unsupported: { label: "Non supporté" },
  invalid: { label: "Invalide" },
};

/** Vrai si l'état est un état connu du Plugin Registry. */
export function isPluginState(state: string): state is PluginState {
  return Object.prototype.hasOwnProperty.call(PLUGIN_STATES, state);
}

/** Libellé d'un état de Plugin ; l'état brut passe s'il est inconnu. */
export function formatPluginState(state: string): string {
  if (!state) {
    return "Inconnu";
  }
  return isPluginState(state) ? PLUGIN_STATES[state].label : state;
}

/** Classe CSS bornée d'un état de Plugin (aucune injection de valeur brute). */
export function pluginStateClass(state: string): string {
  return isPluginState(state) ? state : "unknown";
}
