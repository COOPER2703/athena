/**
 * Modèle des Réglages : sections, brouillon, validation et retour après
 * redémarrage.
 *
 * Module **pur** : aucun DOM, aucun `fetch`, aucun React. Il traduit la réponse
 * de l'API en libellés, prépare l'objet complet qu'un `PUT` exige, calcule les
 * URL des Surfaces déplacées et décide comment la Console revient après un
 * redémarrage. Le rendu de la zone est couvert par les parcours navigateur.
 */

import type { BadgeTone } from "../../shared/ui/Badge";
import type {
  SettingField,
  SettingKind,
  SettingSource,
  SettingsPayload,
  SettingsSurface,
} from "../../api/settings";
import type { SecretStatus } from "../../api/secrets";
import { errorCode, errorStatus } from "../../api/errors";

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

export type SettingsSectionId = "voice" | "memory" | "surfaces" | "advanced";

export interface SettingsSection {
  id: SettingsSectionId;
  title: string;
  summary: string;
  fields: readonly SettingField[];
}

const SECTIONS: ReadonlyArray<{
  id: SettingsSectionId;
  title: string;
  summary: string;
  keys: readonly string[];
}> = [
  {
    id: "voice",
    title: "Voix",
    summary: "La voix et les modèles qui font parler Athena.",
    keys: ["voice", "live_model", "judge_model", "consolidation_model"],
  },
  {
    id: "memory",
    title: "Mémoire",
    summary: "Le profil, la fenêtre de rappel et la Consolidation du Journal.",
    keys: [
      "user_id",
      "short_term_window",
      "recent_events_window",
      "recall_k",
      "recall_window_turns",
      "recall_max_distance",
      "dedup_threshold",
      "journal_retention_days",
      "consolidation_hours",
    ],
  },
  {
    id: "surfaces",
    title: "Surfaces",
    summary: "Les adresses d'écoute de la Console, du Webhook et du transport.",
    keys: [
      "server_host",
      "server_port",
      "webhook_enabled",
      "webhook_host",
      "webhook_port",
      "console_enabled",
      "console_host",
      "console_port",
      "console_trace_buffer",
    ],
  },
  {
    id: "advanced",
    title: "Avancé",
    summary: "Seuils d'authentification, journalisation et chemins protégés.",
    keys: ["timezone", "schedule_db"],
  },
];

const SECTION_OF_KEY: Readonly<Record<string, SettingsSectionId>> =
  Object.fromEntries(
    SECTIONS.flatMap((section) =>
      section.keys.map((key) => [key, section.id] as const),
    ),
  );

/**
 * Range les champs dans les quatre sections, dans l'ordre du serveur.
 *
 * Un réglage inconnu — une version plus récente du serveur — atterrit dans
 * « Avancé » plutôt que de disparaître : aucun champ n'est jamais masqué.
 */
export function settingsSections(
  fields: readonly SettingField[],
): SettingsSection[] {
  return SECTIONS.flatMap((section) => {
    const members = fields.filter(
      (field) => (SECTION_OF_KEY[field.key] ?? "advanced") === section.id,
    );
    return members.length === 0
      ? []
      : [
          {
            id: section.id,
            title: section.title,
            summary: section.summary,
            fields: members,
          },
        ];
  });
}

// ---------------------------------------------------------------------------
// Libellés
// ---------------------------------------------------------------------------

interface SettingMeta {
  label: string;
  hint?: string;
}

const META: Readonly<Record<string, SettingMeta>> = {
  voice: { label: "Voix", hint: "Nom de la voix Gemini, par exemple Leda." },
  live_model: {
    label: "Modèle Live",
    hint: "Modèle utilisé pendant une Session vocale.",
  },
  judge_model: {
    label: "Modèle du juge",
    hint: "Modèle qui arbitre la pertinence d'une réponse.",
  },
  consolidation_model: { label: "Modèle de Consolidation" },
  user_id: { label: "Profil", hint: "Identifiant du profil de mémoire." },
  short_term_window: { label: "Fenêtre courte (tours)" },
  recent_events_window: { label: "Événements récents" },
  recall_k: { label: "Rappels (k)" },
  recall_window_turns: { label: "Fenêtre de rappel (tours)" },
  recall_max_distance: { label: "Distance maximale de rappel" },
  dedup_threshold: { label: "Seuil de déduplication" },
  journal_retention_days: { label: "Rétention du Journal (jours)" },
  consolidation_hours: {
    label: "Heures de Consolidation",
    hint: "Liste d'heures entre 0 et 23, séparées par des virgules.",
  },
  server_host: {
    label: "Hôte du transport",
    hint: "Hôte seul, sans schéma ni chemin.",
  },
  server_port: { label: "Port du transport" },
  webhook_enabled: { label: "Webhook activé" },
  webhook_host: {
    label: "Hôte du Webhook",
    hint: "Hôte seul, sans schéma ni chemin.",
  },
  webhook_port: { label: "Port du Webhook" },
  console_enabled: { label: "Console activée" },
  console_host: {
    label: "Hôte de la Console",
    hint: "Hôte seul, sans schéma ni chemin.",
  },
  console_port: { label: "Port de la Console" },
  console_trace_buffer: { label: "Tampon de traces" },
  ticket_days: { label: "Durée du Ticket (jours)" },
  ticket_refresh_seconds: { label: "Rafraîchissement du Ticket (s)" },
  password_min_length: { label: "Longueur minimale du mot de passe" },
  login_max_attempts: { label: "Tentatives avant blocage" },
  login_block_minutes: { label: "Durée du blocage (min)" },
  log_level: { label: "Niveau de journalisation" },
  timezone: {
    label: "Fuseau horaire de l'instance",
    hint: "Nom IANA, par exemple Europe/Paris. Il donne l'heure locale des prompts et des Planifications.",
  },
  schedule_db: { label: "Base des Planifications" },
  memory_db: { label: "Base mémoire" },
  auth_db: { label: "Base d'authentification" },
  settings_db: { label: "Base des réglages" },
  master_key_file: { label: "Fichier de clé maître" },
  plugins_dir: { label: "Répertoire des Plugins" },
  plugins_state: { label: "État des Plugins" },
  embed_model: { label: "Modèle d'embedding" },
  log_dir: { label: "Répertoire des journaux" },
};

/** Libellé d'un champ ; la clé brute reste lisible pour un réglage inconnu. */
export function settingLabel(key: string): string {
  return META[key]?.label ?? key;
}

/** Aide courte d'un champ : forme attendue et bornes, sans redire le serveur. */
export function settingHint(field: SettingField): string {
  const explicit = META[field.key]?.hint;
  if (explicit) {
    return explicit;
  }
  switch (field.kind) {
    case "bool":
      return "Activé ou désactivé.";
    case "int":
    case "float":
      return boundsLabel(field);
    case "hours":
      return "Liste d'heures entre 0 et 23, séparées par des virgules.";
    case "timezone":
      return "Nom IANA, par exemple Europe/Paris.";
    case "string":
      return field.choices.length > 0
        ? `Valeurs possibles : ${field.choices.join(", ")}.`
        : "";
  }
}

function boundsLabel(field: SettingField): string {
  if (field.minimum !== null && field.maximum !== null) {
    return `Entre ${formatBound(field.minimum)} et ${formatBound(field.maximum)}.`;
  }
  if (field.minimum !== null) {
    return `Minimum ${formatBound(field.minimum)}.`;
  }
  if (field.maximum !== null) {
    return `Maximum ${formatBound(field.maximum)}.`;
  }
  return "";
}

function formatBound(value: number): string {
  return String(value);
}

/** Libellé d'une Surface, pour le dialogue de confirmation. */
export function surfaceLabel(name: string): string {
  switch (name) {
    case "console":
      return "Console";
    case "webhook":
      return "Webhook";
    case "transport":
      return "Transport des Clients";
    default:
      return name;
  }
}

/** Nom français de la source d'une valeur effective. */
export function sourceLabel(source: SettingSource): string {
  switch (source) {
    case "default":
      return "Défaut";
    case "store":
      return "Enregistré";
    case "env":
      return "Environnement";
  }
}

/** Tonalité du badge de source : l'accent reste réservé à une valeur écrite. */
export function sourceTone(source: SettingSource): BadgeTone {
  switch (source) {
    case "default":
      return "neutral";
    case "store":
      return "accent";
    case "env":
      return "warning";
  }
}

/** Explique pourquoi un champ ne peut pas être écrit depuis la Console. */
export function readOnlyReason(field: SettingField): string {
  if (field.source === "env") {
    return `Lecture seule : cette valeur est fixée par la variable d'environnement ${field.env} dans l'environnement du serveur ; elle masque le magasin.`;
  }
  return "Lecture seule : ce chemin est protégé — une faute de frappe ferait démarrer le serveur sur une base vide ou couperait l'accès à la Console.";
}

/** Valeur affichable d'un champ : un booléen se lit en clair. */
export function displayValue(field: SettingField): string {
  if (field.kind === "bool") {
    return isTrue(field.value) ? "Activé" : "Désactivé";
  }
  return field.value === "" ? "—" : field.value;
}

// ---------------------------------------------------------------------------
// Brouillon, différences et validation
// ---------------------------------------------------------------------------

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);
const FALSE_VALUES = new Set(["0", "false", "no", "off"]);
const MAX_STRING_LENGTH = 256;

function isTrue(value: string): boolean {
  return TRUE_VALUES.has(value.trim().toLowerCase());
}

/** Vrai si la Console peut écrire ce champ. */
export function isWritable(field: SettingField): boolean {
  return field.editable && !field.readOnly;
}

/** Champs que l'objet complet d'un `PUT` doit porter. */
export function writableFields(
  fields: readonly SettingField[],
): SettingField[] {
  return fields.filter(isWritable);
}

/**
 * Valeur de référence d'un champ écrivable.
 *
 * Un changement déjà stocké mais pas encore chargé fait foi : le brouillon
 * part de la valeur qui sera active au redémarrage, pas de l'ancienne.
 */
export function baselineValue(field: SettingField): string {
  if (field.pending) {
    return field.pendingValue ?? field.defaultValue;
  }
  return field.value;
}

/** Brouillon initial du formulaire : les seuls champs écrivables. */
export function draftFromFields(
  fields: readonly SettingField[],
): Record<string, string> {
  const draft: Record<string, string> = {};
  for (const field of writableFields(fields)) {
    draft[field.key] = baselineValue(field);
  }
  return draft;
}

/**
 * Forme canonique d'une valeur, alignée sur la validation du serveur.
 *
 * Elle sert à comparer une saisie à sa référence — `05` vaut `5`, `INFO` vaut
 * `info` — et à composer l'objet complet envoyé. Une saisie invalide est
 * seulement élaguée : c'est la validation qui la refuse, jamais la comparaison.
 */
export function canonicalValue(field: SettingField, raw: string): string {
  const value = raw.trim();
  switch (field.kind) {
    case "bool": {
      const lowered = value.toLowerCase();
      if (TRUE_VALUES.has(lowered)) {
        return "1";
      }
      if (FALSE_VALUES.has(lowered)) {
        return "0";
      }
      return value;
    }
    case "int": {
      if (!/^[+-]?\d+$/.test(value)) {
        return value;
      }
      return String(Number.parseInt(value, 10));
    }
    case "float": {
      if (value === "") {
        return value;
      }
      const number = Number(value);
      return Number.isFinite(number) ? String(number) : value;
    }
    case "hours": {
      const hours = parseHours(value);
      return hours === null ? value : hours.join(",");
    }
    case "timezone": {
      return value;
    }
    case "string": {
      if (field.choices.length > 0) {
        return matchChoice(field.choices, value) ?? value;
      }
      return value;
    }
  }
}

/** Clés dont la saisie diffère de leur valeur de référence. */
export function changedKeys(
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
): string[] {
  const changed: string[] = [];
  for (const field of writableFields(fields)) {
    const reference = canonicalValue(field, baselineValue(field));
    const current = canonicalValue(
      field,
      draft[field.key] ?? baselineValue(field),
    );
    if (current !== reference) {
      changed.push(field.key);
    }
  }
  return changed;
}

/** Réinitialise un seul champ vers sa valeur par défaut. */
export function resetSetting(
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
  key: string,
): Record<string, string> {
  const field = fields.find((item) => item.key === key);
  if (field === undefined || !isWritable(field)) {
    return { ...draft };
  }
  return { ...draft, [key]: field.defaultValue };
}

/** Abandonne les modifications : le brouillon repart de l'état de référence. */
export function discardChanges(
  fields: readonly SettingField[],
): Record<string, string> {
  return draftFromFields(fields);
}

function matchChoice(choices: readonly string[], value: string): string | null {
  const lowered = value.toLowerCase();
  return (
    choices.find((choice) => choice.toLowerCase() === lowered) ?? null
  );
}

function parseHours(value: string): number[] | null {
  const parts = value
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part !== "");
  if (parts.length === 0) {
    return null;
  }
  const hours: number[] = [];
  for (const part of parts) {
    if (!/^[+-]?\d+$/.test(part)) {
      return null;
    }
    const hour = Number.parseInt(part, 10);
    if (hour < 0 || hour > 23) {
      return null;
    }
    hours.push(hour);
  }
  return hours;
}

function normalizeBool(raw: string): "1" | "0" | null {
  const value = raw.trim().toLowerCase();
  if (TRUE_VALUES.has(value)) {
    return "1";
  }
  if (FALSE_VALUES.has(value)) {
    return "0";
  }
  return null;
}

function isHostKey(key: string): boolean {
  return key.endsWith("_host");
}

/**
 * Miroir navigateur du contrôle IANA du serveur.
 *
 * `Intl` connaît la base de fuseaux de la plateforme ; une chaîne qui n'y
 * résout pas serait de toute façon refusée par le serveur. Pas d'inventaire
 * embarqué : la règle reste une seule, celle de la plateforme.
 */
function isValidTimezone(value: string): boolean {
  try {
    new Intl.DateTimeFormat("en", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

/**
 * Valide le brouillon complet, en miroir du serveur.
 *
 * Rend un message français par champ fautif ; un objet vide signifie « prêt à
 * envoyer ». Les champs en lecture seule ne sont jamais validés : ils ne
 * partent pas non plus.
 */
export function validateSettings(
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of writableFields(fields)) {
    const message = validateField(
      field,
      draft[field.key] ?? baselineValue(field),
    );
    if (message !== null) {
      errors[field.key] = message;
    }
  }
  return errors;
}

function validateField(field: SettingField, raw: string): string | null {
  const value = raw.trim();
  if (field.kind === "bool") {
    return normalizeBool(value) === null ? "Attendu : « 1 » ou « 0 »." : null;
  }
  if (field.kind === "int") {
    if (!/^[+-]?\d+$/.test(value)) {
      return "Un nombre entier est attendu.";
    }
    return checkBounds(field, Number.parseInt(value, 10));
  }
  if (field.kind === "float") {
    const number = Number(value);
    if (value === "" || !Number.isFinite(number)) {
      return "Un nombre décimal fini est attendu.";
    }
    return checkBounds(field, number);
  }
  if (field.kind === "hours") {
    return parseHours(value) === null
      ? "Attendu : une liste d'heures entre 0 et 23, séparées par des virgules."
      : null;
  }
  if (field.kind === "timezone") {
    if (value === "") {
      return "Un nom de fuseau IANA est attendu, par exemple Europe/Paris.";
    }
    return isValidTimezone(value)
      ? null
      : "Nom de fuseau IANA attendu, par exemple Europe/Paris.";
  }
  if (value === "") {
    return "Une valeur est attendue.";
  }
  if (value.length > MAX_STRING_LENGTH) {
    return "Cette valeur est trop longue.";
  }
  if (field.choices.length > 0 && matchChoice(field.choices, value) === null) {
    return `Valeur non reconnue : ${field.choices.join(", ")}.`;
  }
  if (isHostKey(field.key)) {
    if (value.includes("/") || value.includes("://")) {
      return "Indiquez un hôte seul, sans schéma ni chemin.";
    }
    if (/\s/.test(value)) {
      return "Un hôte ne contient pas d'espace.";
    }
  }
  return null;
}

function checkBounds(field: SettingField, number: number): string | null {
  if (field.minimum !== null && number < field.minimum) {
    return `La valeur minimale est ${formatBound(field.minimum)}.`;
  }
  if (field.maximum !== null && number > field.maximum) {
    return `La valeur maximale est ${formatBound(field.maximum)}.`;
  }
  return null;
}

/** Objet complet et canonique qu'un `PUT` doit porter. */
export function settingsPayload(
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
): Record<string, string> {
  const values: Record<string, string> = {};
  for (const field of writableFields(fields)) {
    values[field.key] = canonicalValue(
      field,
      draft[field.key] ?? baselineValue(field),
    );
  }
  return values;
}

// ---------------------------------------------------------------------------
// Surfaces, confirmation d'URL et redémarrage
// ---------------------------------------------------------------------------

export interface SurfaceChange {
  name: string;
  scheme: string;
  currentUrl: string;
  nextUrl: string;
}

function fieldByKey(
  fields: readonly SettingField[],
  key: string,
): SettingField | null {
  return fields.find((field) => field.key === key) ?? null;
}

function activeValue(
  fields: readonly SettingField[],
  key: string,
): string | null {
  const field = fieldByKey(fields, key);
  return field === null ? null : field.value;
}

function draftValue(
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
  key: string,
): string | null {
  const field = fieldByKey(fields, key);
  if (field === null) {
    return null;
  }
  if (!isWritable(field)) {
    return field.value;
  }
  return draft[key] ?? baselineValue(field);
}

function urlPart(url: string, part: "hostname" | "port"): string {
  try {
    const parsed = new URL(url);
    return part === "hostname" ? parsed.hostname : parsed.port;
  } catch {
    return "";
  }
}

/** URL d'une surface, avec crochets pour une adresse IPv6. */
export function surfaceUrl(scheme: string, host: string, port: string): string {
  let normalizedHost = host.trim();
  if (normalizedHost.startsWith("[") && normalizedHost.endsWith("]")) {
    normalizedHost = normalizedHost.slice(1, -1);
  }
  if (normalizedHost.includes(":") && !normalizedHost.startsWith("[")) {
    normalizedHost = `[${normalizedHost}]`;
  }
  return `${scheme}://${normalizedHost}:${port.trim()}`;
}

function surfaceNextUrl(
  surface: SettingsSurface,
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
): string {
  const currentHost =
    activeValue(fields, surface.hostKey) ||
    urlPart(surface.url, "hostname");
  const currentPort =
    activeValue(fields, surface.portKey) || urlPart(surface.url, "port");
  const host = draftValue(fields, draft, surface.hostKey) ?? currentHost;
  const port = draftValue(fields, draft, surface.portKey) ?? currentPort;
  return surfaceUrl(surface.scheme, host, port);
}

/** Surfaces dont l'adresse changerait si le brouillon était appliqué. */
export function surfaceChanges(
  payload: SettingsPayload,
  draft: Readonly<Record<string, string>>,
): SurfaceChange[] {
  const changes: SurfaceChange[] = [];
  for (const surface of payload.surfaces) {
    const nextUrl = surfaceNextUrl(surface, payload.fields, draft);
    const currentUrl =
      surface.url ||
      surfaceUrl(
        surface.scheme,
        activeValue(payload.fields, surface.hostKey) || "",
        activeValue(payload.fields, surface.portKey) || "",
      );
    if (!sameUrl(currentUrl, nextUrl)) {
      changes.push({
        name: surface.name,
        scheme: surface.scheme,
        currentUrl,
        nextUrl,
      });
    }
  }
  return changes;
}

function sameUrl(left: string, right: string): boolean {
  return urlIdentity(left) === urlIdentity(right);
}

function urlIdentity(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.protocol}//${parsed.host}`.toLowerCase();
  } catch {
    return url.trim().toLowerCase();
  }
}

/** URL exactes que `confirm_url` doit porter pour un brouillon déplacé. */
export function confirmUrls(
  payload: SettingsPayload,
  draft: Readonly<Record<string, string>>,
): string[] {
  return surfaceChanges(payload, draft).map((change) => change.nextUrl);
}

/** Champs en attente de redémarrage, dans l'ordre du serveur. */
export function pendingFields(payload: SettingsPayload): SettingField[] {
  return payload.fields.filter((field) => field.pending);
}

/** Résumé du bandeau : « N changements, redémarrage requis ». */
export function pendingSummary(payload: SettingsPayload): string {
  const count = pendingFields(payload).length;
  if (count === 0) {
    return "";
  }
  return `${count} changement${count > 1 ? "s" : ""}, redémarrage requis`;
}

/** Valeur qui sera active au redémarrage pour un champ en attente. */
export function pendingValue(field: SettingField): string {
  return field.pendingValue ?? field.defaultValue;
}

/**
 * URL que la Console servira après application, ou `null` si son adresse ne
 * change pas. Un port revenu au défaut vaut `null` côté serveur : on le rend
 * ici comme la valeur par défaut.
 */
export function consoleUrlAfterRestart(
  payload: SettingsPayload,
): string | null {
  const surface = payload.surfaces.find((item) => item.name === "console");
  if (surface === undefined) {
    return null;
  }
  const hostField = fieldByKey(payload.fields, surface.hostKey);
  const portField = fieldByKey(payload.fields, surface.portKey);
  const hostChanged = hostField?.pending === true;
  const portChanged = portField?.pending === true;
  if (!hostChanged && !portChanged) {
    return null;
  }
  const host =
    (hostChanged
      ? (hostField?.pendingValue ?? hostField?.defaultValue ?? "")
      : (hostField?.value ?? "")) || urlPart(surface.url, "hostname");
  const port =
    (portChanged
      ? (portField?.pendingValue ?? portField?.defaultValue ?? "")
      : (portField?.value ?? "")) || urlPart(surface.url, "port");
  return surfaceUrl(surface.scheme, host, port);
}

/** Comment la Console revient après enregistrement. */
export type RestartPlan =
  | { kind: "none" }
  | { kind: "manual"; command: string }
  | { kind: "poll" }
  | { kind: "moved"; url: string };

/**
 * Décide du retour : rien à faire, commande à exécuter, attente active, ou
 * nouvelle adresse à ouvrir. Le déplacement prime sur l'attente — un onglet
 * resté sur l'ancien port ne verra jamais le serveur revenir.
 */
export function restartPlan(
  payload: SettingsPayload,
  currentOrigin: string,
): RestartPlan {
  if (!payload.restart.required) {
    return { kind: "none" };
  }
  if (!payload.restart.automatic) {
    return { kind: "manual", command: payload.restart.command };
  }
  const next = consoleUrlAfterRestart(payload);
  if (next !== null && urlIdentity(next) !== urlIdentity(currentOrigin)) {
    return { kind: "moved", url: next };
  }
  return { kind: "poll" };
}

// ---------------------------------------------------------------------------
// Secrets et indisponibilité
// ---------------------------------------------------------------------------

/** Résumé d'un Secret : jamais de valeur, seulement l'état et la date. */
export function secretSummary(
  secret: SecretStatus,
  formatDate: (seconds: number | null) => string,
): string {
  if (!secret.configured) {
    return "Non configuré";
  }
  if (secret.updatedAt === null) {
    return "Configuré";
  }
  return `Configuré le ${formatDate(secret.updatedAt)}`;
}

/** Vrai quand le magasin de réglages n'est pas ouvert sur cette instance. */
export function settingsUnavailable(error: unknown): boolean {
  const code = errorCode(error);
  if (code === "settings_unavailable") {
    return true;
  }
  return errorStatus(error) === 404 && code === "not_found";
}

/** Clé du champ booléen et libellé de son contrôle. */
export const BOOLEAN_CHOICES: ReadonlyArray<{ value: string; label: string }> = [
  { value: "1", label: "Activé" },
  { value: "0", label: "Désactivé" },
];

/** Type de clavier attendu pour un champ texte. */
export function inputModeFor(kind: SettingKind): "numeric" | "decimal" | "text" {
  switch (kind) {
    case "int":
      return "numeric";
    case "float":
      return "decimal";
    default:
      return "text";
  }
}
