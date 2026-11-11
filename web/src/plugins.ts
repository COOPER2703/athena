/**
 * Fiche d'un plugin installé : normalisation défensive, regroupement et
 * registre (compteurs, recherche, filtres, motifs de rail).
 *
 * L'instantané du serveur reste en `snake_case` et peut être partiel (une
 * trame ancienne, un dossier jamais lu, un manifeste cassé). Ce module est pur
 * et sans DOM : il borne chaque champ à une valeur affichable, et regroupe la
 * matière de la fiche (identité, Tools, Instruction, panne) pour que le
 * composant n'ait aucune logique à porter.
 *
 * Source de vérité du diagnostic : quand le serveur publie `ready`, il fait foi
 * pour la configuration et les secrets — l'interface ne recalcule jamais deux
 * vérités concurrentes. Les **manques** restent déduits des listes
 * (`required` + `value`/`present`) pour les nommer, et un manque visible
 * l'emporte toujours sur un `ready` qui prétendrait le contraire. Sans
 * `ready` (trame ancienne), on retombe sur la déduction locale ; le diagnostic
 * calculé sait aussi dire « non évaluable ».
 */

import { normalizeText } from "./filters";

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
  /**
   * Marqueurs publiés par le serveur ; `null` sur une trame ancienne.
   *
   * Ils font foi pour la configuration et les secrets dès qu'ils sont
   * présents : l'interface déduit encore les manques pour les nommer, mais ne
   * conclut jamais « satisfait » contre un manque visible.
   */
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

/** Un marqueur prêt à rendre : libellé d'état et manques déjà résolus. */
export interface PluginMarkerView {
  key: "manifest" | "config" | "secrets";
  label: string;
  stateLabel: string;
  stateClass: PluginMarkerState;
  note: string | null;
  gaps: string[];
}

/** Fiche groupée d'un plugin, prête à rendre. */
export interface PluginSheet {
  /** Identité déclarée, `[]` quand aucune Déclaration n'a été lue. */
  identity: PluginFact[];
  tools: string[];
  instruction: string;
  failure: string | null;
  /** Raison à afficher hors raison d'invalidité, déjà dédoublonnée. */
  failureMessage: string | null;
  invalidReason: string | null;
  retry: boolean;
  loaded: boolean;
  declarationMissing: boolean;
  config: PluginConfigRow[];
  secrets: PluginSecretRow[];
  diagnostic: PluginDiagnostic;
  /** Marqueurs affichés ; le manifeste invalide n'y répète pas sa raison. */
  markers: PluginMarkerView[];
  /** Note affichée quand un marqueur reste non évaluable. */
  diagnosticNote: string | null;
  /** Texte d'absence de configuration, `null` dès qu'un champ est déclaré. */
  configNotice: string | null;
  /** Texte d'absence de secret, `null` dès qu'un secret est déclaré. */
  secretsNotice: string | null;
  /** Texte d'absence de Tool, `null` dès qu'un Tool est exposé. */
  toolsNotice: string | null;
  /** Texte d'absence d'Instruction, `null` quand elle existe. */
  instructionNote: string | null;
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

/**
 * Un marqueur de réglage : le `ready` du serveur fait foi, les manques nommés.
 *
 * Un `ready` contradictoire n'annonce jamais « satisfait » quand un manque est
 * visible : le manque gagne. Sans `ready` (trame ancienne), la déduction
 * locale reste la seule information disponible.
 */
function requirementMarker(
  key: "config" | "secrets",
  label: string,
  evaluable: boolean,
  ready: boolean | undefined,
  gaps: string[],
  unevaluableNote: string,
): PluginMarker {
  if (!evaluable) {
    return {
      key,
      label,
      state: "unknown",
      gaps: [],
      note: unevaluableNote,
    };
  }
  const satisfied = (ready ?? true) && gaps.length === 0;
  return {
    key,
    label,
    state: satisfied ? "ok" : "missing",
    gaps,
    note: null,
  };
}

/** Diagnostic « prêt à activer » : trois marqueurs et les manques nommés.
 *
 * Le serveur ne fournit aucune phrase : les manques se déduisent des
 * `required`, des `value`/`present` et du manifeste. Son `ready` fait foi pour
 * la configuration et les secrets, mais un manque visible l'emporte. Un
 * dossier sans manifeste lisible n'est pas « prêt » et n'invente pas de
 * manque : ses marqueurs sont *non évaluables*, une distinction que
 * l'interface doit rendre visible.
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
  const config = requirementMarker(
    "config",
    "Configuration requise",
    manifestKnown,
    plugin.ready?.config,
    configGapList,
    unevaluable,
  );
  const secrets = requirementMarker(
    "secrets",
    "Secrets requis",
    manifestKnown,
    plugin.ready?.secrets,
    secretGapList,
    unevaluable,
  );

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

function markerView(marker: PluginMarker): PluginMarkerView {
  return {
    key: marker.key,
    label: marker.label,
    stateLabel: formatMarkerState(marker.state),
    stateClass: marker.state,
    note: marker.note,
    // La raison d'un manifeste invalide est affichée plus haut : la répéter
    // dans les manques ferait lire deux fois la même phrase.
    gaps: marker.key === "manifest" ? [] : marker.gaps,
  };
}

/**
 * Regroupe la fiche : identité déclarée, réglages, diagnostic, Tools et panne.
 *
 * La ligne `api_version` est toujours rendue dès qu'une Déclaration existe :
 * une valeur inconnue s'affiche « — », jamais « 0 » qui ferait passer une
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
    identity.push({
      label: "api_version",
      value: declaration.apiVersion > 0 ? String(declaration.apiVersion) : "—",
      mono: true,
    });
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
  const invalidReason = declaration?.invalidReason ?? null;
  const diagnostic = pluginDiagnostic(plugin);
  const declarationMissing = declaration === null;
  // Une plateforme non supportée n'est jamais activable ici : le diagnostic
  // reste rendu pour les états de #158, mais la note empêche de lire ses
  // marqueurs satisfaits comme une promesse d'activation.
  const unsupportedNote =
    plugin.state === "unsupported"
      ? "Plateforme non supportée : l'activation n'est pas possible ici."
      : null;
  return {
    identity,
    tools: plugin.tools,
    instruction: plugin.instruction,
    failure: plugin.reason,
    failureMessage:
      plugin.reason !== null && plugin.reason !== invalidReason
        ? plugin.reason
        : null,
    invalidReason,
    retry: plugin.retry,
    loaded: plugin.state === "activated",
    declarationMissing,
    config: plugin.config.map(configRow),
    secrets: plugin.secrets.map(secretRow),
    diagnostic,
    markers: [
      markerView(diagnostic.manifest),
      markerView(diagnostic.config),
      markerView(diagnostic.secrets),
    ],
    diagnosticNote:
      unsupportedNote ??
      (diagnostic.unknown
        ? "Diagnostic partiel : des exigences restent illisibles."
        : null),
    configNotice:
      plugin.config.length > 0
        ? null
        : declarationMissing
          ? "Réglages inconnus : aucune Déclaration lue."
          : "Aucun champ de configuration déclaré.",
    secretsNotice:
      plugin.secrets.length > 0
        ? null
        : declarationMissing
          ? "Secrets inconnus : aucune Déclaration lue."
          : "Aucun secret déclaré.",
    toolsNotice: plugin.tools.length > 0 ? null : "Aucun Tool exposé.",
    instructionNote: instructionNotice(plugin),
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

const PLUGIN_STATE_LABELS: Readonly<Record<PluginState, string>> = {
  activated: "Activé",
  failed: "En échec",
  missing: "Manquant",
  inactive: "Inactif",
  unsupported: "Non supporté",
  invalid: "Invalide",
};

/** Les six États de service, dans l'ordre du glossaire du Plugin Registry. */
export const PLUGIN_STATES: readonly PluginState[] = [
  "activated",
  "failed",
  "missing",
  "inactive",
  "unsupported",
  "invalid",
];

/** Vrai si l'état est un état connu du Plugin Registry. */
export function isPluginState(state: string): state is PluginState {
  return Object.prototype.hasOwnProperty.call(PLUGIN_STATE_LABELS, state);
}

/** Libellé d'un état de Plugin ; l'état brut passe s'il est inconnu. */
export function formatPluginState(state: string): string {
  if (!state) {
    return "Inconnu";
  }
  return isPluginState(state) ? PLUGIN_STATE_LABELS[state] : state;
}

/** Classe CSS bornée d'un état de Plugin (aucune injection de valeur brute). */
export function pluginStateClass(state: string): string {
  return isPluginState(state) ? state : "unknown";
}
/** Compteurs de l'en-tête, recalculés à chaque instantané (jamais figés). */
export interface PluginCounters {
  /** Tous les dossiers installés, quel que soit leur état. */
  discovered: number;
  /** État `activated` uniquement : un plugin en échec n'est pas en service. */
  activated: number;
  /** État `failed` uniquement ; `missing` reste un manque, pas un échec de service. */
  failed: number;
}

/** Une cellule de l'en-tête, avec son libellé français. */
export interface PluginCounterItem {
  key: keyof PluginCounters;
  label: string;
  value: number;
}

const COUNTER_LABELS: Readonly<Record<keyof PluginCounters, string>> = {
  discovered: "Découverts",
  activated: "En service",
  failed: "En échec",
};

/** Compte les plugins du registre, exactement comme le flux les publie. */
export function pluginCounters(
  plugins: readonly PluginInfo[],
): PluginCounters {
  let activated = 0;
  let failed = 0;
  for (const plugin of plugins) {
    if (plugin.state === "activated") {
      activated += 1;
    } else if (plugin.state === "failed") {
      failed += 1;
    }
  }
  return { discovered: plugins.length, activated, failed };
}

/** Ordonne les compteurs pour l'en-tête : découverts, en service, en échec. */
export function pluginCounterItems(
  counters: PluginCounters,
): PluginCounterItem[] {
  return (["discovered", "activated", "failed"] as const).map((key) => ({
    key,
    label: COUNTER_LABELS[key],
    value: counters[key],
  }));
}

/** Filtre du registre ; un état vide signifie « tous », un texte vide « tout ». */
export interface PluginFilter {
  state: string;
  text: string;
}

/** Filtre neutre : tout le registre, dans l'ordre du flux. */
export const EMPTY_PLUGIN_FILTER: PluginFilter = { state: "", text: "" };

/** Options du filtre par État de service, « tous » en tête. */
export const PLUGIN_STATE_FILTERS: ReadonlyArray<{
  value: string;
  label: string;
}> = [
  { value: "", label: "Tous les états" },
  ...PLUGIN_STATES.map((state) => ({
    value: state,
    label: formatPluginState(state),
  })),
];

/** Vrai dès qu'un critère restreint la liste. */
export function pluginFilterActive(filter: PluginFilter): boolean {
  return filter.state !== "" || filter.text.trim() !== "";
}

/**
 * Texte dans lequel la recherche rapproche : nom, description, noms de Tools.
 *
 * L'état, le dossier et la raison d'échec en sont exclus : chercher « failed »
 * ne doit pas ramener tous les plugins en échec, et une variable de chemin ne
 * doit pas se confondre avec un nom de Tool.
 */
function searchableText(plugin: PluginInfo): string {
  return normalizeText(
    [plugin.name, plugin.declaration?.description ?? "", ...plugin.tools].join(
      " ",
    ),
  );
}

/** Vrai si le plugin porte le texte cherché, casse et accents ignorés. */
export function matchesPluginSearch(
  plugin: PluginInfo,
  text: string,
): boolean {
  const needle = normalizeText(text.trim());
  return needle === "" || searchableText(plugin).includes(needle);
}

/** Vrai si le plugin passe le filtre d'état **et** la recherche. */
export function matchesPluginFilter(
  plugin: PluginInfo,
  filter: PluginFilter,
): boolean {
  if (filter.state !== "" && plugin.state !== filter.state) {
    return false;
  }
  return matchesPluginSearch(plugin, filter.text);
}

/** Lignes visibles, dans l'ordre du flux, sans tri ni copie de l'instantané. */
export function filterPlugins(
  plugins: readonly PluginInfo[],
  filter: PluginFilter,
): PluginInfo[] {
  return plugins.filter((plugin) => matchesPluginFilter(plugin, filter));
}

/** Texte du résultat, annoncé dans une zone `aria-live`. */
export interface PluginResultSummary {
  label: string;
  /** Vrai quand aucune ligne ne peut être rendue : le texte le dit alors. */
  empty: boolean;
}

function plural(count: number, singular: string, pluralForm: string): string {
  return count > 1 ? pluralForm : singular;
}

/**
 * Phrase de résultat, y compris le cas vide explicite.
 *
 * Un registre vide et un registre filtré jusqu'à vide ne disent pas la même
 * chose : le premier n'a rien à montrer, le second invite à relâcher le filtre.
 */
export function pluginResultSummary(
  filter: PluginFilter,
  counts: { total: number; shown: number },
): PluginResultSummary {
  if (counts.total === 0) {
    return { label: "Aucun Plugin installé.", empty: true };
  }
  if (counts.shown === 0) {
    return {
      label: "Aucun Plugin ne correspond à la recherche et au filtre d'état.",
      empty: true,
    };
  }
  if (!pluginFilterActive(filter)) {
    return {
      label: `${counts.total} ${plural(
        counts.total,
        "Plugin installé",
        "Plugins installés",
      )}.`,
      empty: false,
    };
  }
  return {
    label: `${counts.shown} ${plural(
      counts.shown,
      "Plugin affiché",
      "Plugins affichés",
    )} sur ${counts.total}.`,
    empty: false,
  };
}

/**
 * Motif de rail d'un État de service.
 *
 * Six motifs se distinguent en noir et blanc : plein, creux, pointillé, brisé,
 * hachuré, double. La couleur ne porte jamais l'information seule, et un état
 * inconnu (trame partielle) retombe sur le creux plutôt que d'inventer un
 * septième motif ou d'injecter la valeur brute dans une classe.
 */
export type PluginRailMotif =
  | "solid"
  | "hollow"
  | "dotted"
  | "broken"
  | "hatched"
  | "double";

const RAIL_MOTIFS: Readonly<Record<PluginState, PluginRailMotif>> = {
  activated: "solid",
  inactive: "hollow",
  missing: "dotted",
  failed: "broken",
  unsupported: "hatched",
  invalid: "double",
};

/** Motif de rail borné pour un état ; `hollow` si l'état est inconnu. */
export function pluginRailMotif(state: string): PluginRailMotif {
  return pluginStateClass(state) === "unknown"
    ? "hollow"
    : RAIL_MOTIFS[state as PluginState];
}

/** Une ligne du registre, prête à rendre sans décision dans le composant. */
export interface PluginRow {
  name: string;
  /** Nom affichable, jamais vide. */
  nameLabel: string;
  stateLabel: string;
  /** Classe bornée de l'état, pour colorer le libellé sans jamais le remplacer. */
  stateClass: string;
  motif: PluginRailMotif;
  /** Description affichable, avec un manque dit explicitement. */
  descriptionLabel: string;
  /** « Aucun Tool », « 1 Tool » ou « N Tools ». */
  toolsLabel: string;
}

/** Regroupe ce qu'une ligne affiche : identité courte, état et Tools. */
export function pluginRow(plugin: PluginInfo): PluginRow {
  const description = plugin.declaration?.description ?? "";
  const toolCount = plugin.tools.length;
  let toolsLabel = "Aucun Tool";
  if (toolCount === 1) {
    toolsLabel = "1 Tool";
  } else if (toolCount > 1) {
    toolsLabel = `${toolCount} Tools`;
  }
  return {
    name: plugin.name,
    nameLabel: plugin.name || "(sans nom)",
    stateLabel: formatPluginState(plugin.state),
    stateClass: pluginStateClass(plugin.state),
    motif: pluginRailMotif(plugin.state),
    descriptionLabel: description || "Aucune description déclarée.",
    toolsLabel,
  };
}
