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

/** Champ de configuration déclaré par un manifeste, avec sa valeur effective. */
export interface PluginConfigField {
  key: string;
  /** `string` ou `url` ; la Console juge une URL avec ce type. */
  type: string;
  required: boolean;
  /** `null` quand la clé est laissée au défaut du plugin, jamais `""`. */
  value: string | null;
}

/** Secret attendu : clé, variable exacte et présence, jamais la valeur. */
export interface PluginSecretField {
  key: string;
  variable: string;
  required: boolean;
  present: boolean;
}

/** Les trois marqueurs distincts du diagnostic « prêt à activer ». */
export interface PluginReady {
  manifest: boolean;
  config: boolean;
  secrets: boolean;
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
  config: PluginConfigField[];
  secrets: PluginSecretField[];
  /** `null` quand l'instantané ne porte pas de diagnostic exploitable. */
  ready: PluginReady | null;
}

/** Une ligne de la fiche : libellé, valeur, et police monospace éventuelle. */
export interface PluginFact {
  label: string;
  value: string;
  mono?: boolean;
}

/** Ligne de configuration prête à rendre : la valeur ou le défaut du plugin. */
export interface PluginConfigRow {
  key: string;
  type: string;
  required: boolean;
  value: string | null;
  /** Valeur effective, ou « Défaut du plugin » quand `value` est `null`. */
  valueLabel: string;
  isDefault: boolean;
}

/** Ligne de secret : la variable et sa seule présence, jamais sa valeur. */
export interface PluginSecretRow {
  key: string;
  variable: string;
  required: boolean;
  present: boolean;
  /** « Présente » ou « Absente ». */
  presenceLabel: string;
}

/** État d'un marqueur : satisfait, manquant, ou non évaluable. */
export type PluginMarkerState = "ok" | "missing" | "unknown";

/** Un des trois marqueurs du diagnostic, avec les manques qu'il nomme. */
export interface PluginMarker {
  key: "manifest" | "config" | "secrets";
  label: string;
  state: PluginMarkerState;
  /** Manques nommés, vide quand le marqueur est satisfait ou non évaluable. */
  gaps: string[];
  /** Précision affichée quand le marqueur n'est pas évaluable. */
  note: string | null;
}

/** Diagnostic « prêt à activer » déduit des listes, jamais de phrases serveur. */
export interface PluginDiagnostic {
  manifest: PluginMarker;
  config: PluginMarker;
  secrets: PluginMarker;
  /** Vrai si les trois marqueurs sont satisfaits. */
  ready: boolean;
  /** Vrai si un marqueur n'a pas pu être évalué faute de manifeste lisible. */
  unknown: boolean;
  /** Tous les manques, dans l'ordre manifeste, configuration, secrets. */
  gaps: string[];
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
  config: PluginConfigRow[];
  secrets: PluginSecretRow[];
  diagnostic: PluginDiagnostic;
  /** Vrai dès qu'un champ de configuration ou un secret est déclaré. */
  hasRequirements: boolean;
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

function normalizeConfigField(raw: unknown): PluginConfigField | null {
  if (!isRecord(raw)) {
    return null;
  }
  const key = asText(raw.key);
  if (key === "") {
    return null;
  }
  // Une valeur vide n'a jamais signifié « défaut » côté serveur : elle est
  // ramenée à `null`, comme le fait le chargeur.
  const value = typeof raw.value === "string" ? raw.value : null;
  return {
    key,
    type: asText(raw.type) || "string",
    required: raw.required === true,
    value: value === null || value === "" ? null : value,
  };
}

function normalizeSecretField(raw: unknown): PluginSecretField | null {
  if (!isRecord(raw)) {
    return null;
  }
  const key = asText(raw.key);
  if (key === "") {
    return null;
  }
  // Seuls la clé, la variable et la présence sortent : `value` est ignoré
  // même si une trame malformée en portait une.
  return {
    key,
    variable: asText(raw.variable),
    required: raw.required === true,
    present: raw.present === true,
  };
}

function normalizeReady(raw: unknown): PluginReady | null {
  if (!isRecord(raw)) {
    return null;
  }
  return {
    manifest: raw.manifest === true,
    config: raw.config === true,
    secrets: raw.secrets === true,
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
    config: asList(raw.config)
      .map(normalizeConfigField)
      .filter((field): field is PluginConfigField => field !== null),
    secrets: asList(raw.secrets)
      .map(normalizeSecretField)
      .filter((field): field is PluginSecretField => field !== null),
    ready: normalizeReady(raw.ready),
  };
}

/** Normalise la liste `plugins` d'un instantané, en écartant les non-objets. */
export function normalizePlugins(raw: unknown): PluginInfo[] {
  return asList(raw)
    .map(normalizePlugin)
    .filter((plugin): plugin is PluginInfo => plugin !== null);
}

/** Libellé français de l'état d'un marqueur du diagnostic. */
export function formatMarkerState(state: PluginMarkerState): string {
  if (state === "ok") {
    return "Satisfait";
  }
  return state === "missing" ? "Manque" : "Non évaluable";
}

/**
 * Valide une URL déclarée `url`, avec la même règle que le chargeur.
 *
 * `new URL` refuse les valeurs sans schéma ; on exige en plus `http(s)` et un
 * hôte, pour que l'interface nomme exactement ce que le démarrage refuserait.
 */
function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      url.hostname !== ""
    );
  } catch {
    return false;
  }
}

function configGaps(plugin: PluginInfo): string[] {
  const gaps: string[] = [];
  for (const field of plugin.config) {
    if (field.value === null) {
      if (field.required) {
        gaps.push(`\`${field.key}\` requis non renseigné`);
      }
      continue;
    }
    if (field.type === "url" && !isHttpUrl(field.value)) {
      gaps.push(`\`${field.key}\` n'est pas une URL http(s) valide`);
    }
  }
  return gaps;
}

function secretGaps(plugin: PluginInfo): string[] {
  return plugin.secrets
    .filter((field) => field.required && !field.present)
    .map((field) => `\`${field.variable || field.key}\` absente`);
}

/** Diagnostic « prêt à activer » : trois marqueurs et les manques nommés.
 *
 * Le serveur ne fournit aucune phrase : tout se déduit des `required`, des
 * `value`/`present` et du manifeste. Un dossier sans manifeste lisible n'est
 * pas « prêt » et n'invente pas de manque : ses marqueurs de configuration et
 * de secrets sont *non évaluables*, une distinction que l'interface doit
 * rendre visible.
 */
export function pluginDiagnostic(plugin: PluginInfo): PluginDiagnostic {
  const declaration = plugin.declaration;
  const manifestKnown =
    declaration !== null && declaration.invalidReason === null;

  let manifest: PluginMarker;
  if (declaration === null) {
    manifest = {
      key: "manifest",
      label: "Manifeste",
      state: "unknown",
      gaps: [],
      note: "Aucun manifeste lu : diagnostic non évaluable.",
    };
  } else if (declaration.invalidReason !== null) {
    manifest = {
      key: "manifest",
      label: "Manifeste",
      state: "missing",
      gaps: [declaration.invalidReason],
      note: null,
    };
  } else {
    manifest = {
      key: "manifest",
      label: "Manifeste",
      state: "ok",
      gaps: [],
      note: null,
    };
  }

  const unevaluable =
    "Non évaluable : le manifeste ne permet pas de lire les exigences.";
  const configGapList = manifestKnown ? configGaps(plugin) : [];
  const secretGapList = manifestKnown ? secretGaps(plugin) : [];
  const config: PluginMarker = manifestKnown
    ? {
        key: "config",
        label: "Configuration requise",
        state: configGapList.length > 0 ? "missing" : "ok",
        gaps: configGapList,
        note: null,
      }
    : {
        key: "config",
        label: "Configuration requise",
        state: "unknown",
        gaps: [],
        note: unevaluable,
      };
  const secrets: PluginMarker = manifestKnown
    ? {
        key: "secrets",
        label: "Secrets requis",
        state: secretGapList.length > 0 ? "missing" : "ok",
        gaps: secretGapList,
        note: null,
      }
    : {
        key: "secrets",
        label: "Secrets requis",
        state: "unknown",
        gaps: [],
        note: unevaluable,
      };

  const markers = [manifest, config, secrets];
  return {
    manifest,
    config,
    secrets,
    ready: markers.every((marker) => marker.state === "ok"),
    unknown: markers.some((marker) => marker.state === "unknown"),
    gaps: markers.flatMap((marker) => marker.gaps),
  };
}

function configRow(field: PluginConfigField): PluginConfigRow {
  return {
    key: field.key,
    type: field.type,
    required: field.required,
    value: field.value,
    valueLabel: field.value === null ? "Défaut du plugin" : field.value,
    isDefault: field.value === null,
  };
}

function secretRow(field: PluginSecretField): PluginSecretRow {
  return {
    key: field.key,
    variable: field.variable,
    required: field.required,
    present: field.present,
    presenceLabel: field.present ? "Présente" : "Absente",
  };
}

/**
 * Regroupe la fiche : identité déclarée, réglages, diagnostic, Tools et panne.
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
    config: plugin.config.map(configRow),
    secrets: plugin.secrets.map(secretRow),
    diagnostic: pluginDiagnostic(plugin),
    hasRequirements: plugin.config.length > 0 || plugin.secrets.length > 0,
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
