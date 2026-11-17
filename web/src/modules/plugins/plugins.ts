/**
 * Modèle d'affichage de la zone Plugins.
 *
 * Module **pur** : aucun DOM, aucun `fetch`, aucun React. Il traduit les états
 * du registre en libellés, prépare le brouillon de configuration et valide une
 * saisie avant l'envoi, en miroir des règles du serveur (URL `http(s)`, champ
 * requis). Le rendu de la zone reste couvert par les parcours navigateur.
 */

import type { BadgeTone } from "../../shared/ui/Badge";
import type {
  PluginConfigField,
  PluginEntry,
  PluginSecretField,
  PluginState,
} from "../../api/plugins";

/** Libellé français d'un état de chargement. */
export function pluginStateLabel(state: PluginState): string {
  switch (state) {
    case "activated":
      return "Actif";
    case "failed":
      return "En échec";
    case "missing":
      return "Introuvable";
    case "disabled":
      return "Inactif";
  }
}

/** Tonalité du badge d'état : l'accent est réservé à l'état actif. */
export function pluginStateTone(state: PluginState): BadgeTone {
  switch (state) {
    case "activated":
      return "accent";
    case "failed":
    case "missing":
      return "danger";
    case "disabled":
      return "neutral";
  }
}

/** Libellé d'une plateforme déclarée par le manifeste. */
export function platformLabel(platform: string): string {
  switch (platform) {
    case "server":
      return "Serveur";
    case "desktop":
      return "Bureau";
    case "android":
      return "Android";
    case "ios":
      return "iOS";
    case "":
      return "Inconnue";
    default:
      return platform;
  }
}

/** Rappel affiché après une écriture : rien n'agit avant un redémarrage. */
export const RESTART_NOTICE =
  "Effectif au redémarrage : rien n'est rechargé à chaud.";

/** Vrai si le Plugin expose au moins un Tool au modèle. */
export function hasTools(plugin: PluginEntry): boolean {
  return plugin.tools.length > 0;
}

/** Brouillon initial du formulaire de configuration. */
export function configDraft(
  fields: readonly PluginConfigField[],
): Record<string, string> {
  const draft: Record<string, string> = {};
  for (const field of fields) {
    draft[field.key] = field.value;
  }
  return draft;
}

/**
 * Valide le brouillon de configuration, en miroir du serveur.
 *
 * Rend un message français par champ fautif ; un objet vide signifie « prêt à
 * envoyer ». Seuls les champs déclarés par le manifeste sont considérés.
 */
export function validateConfig(
  fields: readonly PluginConfigField[],
  draft: Readonly<Record<string, string>>,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const value = (draft[field.key] ?? "").trim();
    if (field.required && value === "") {
      errors[field.key] = "Ce champ est obligatoire.";
      continue;
    }
    if (field.type === "url" && value !== "" && !isHttpUrl(value)) {
      errors[field.key] = "Attendu : une URL http(s) complète.";
    }
  }
  return errors;
}

/** Charge utile d'un formulaire : les seuls champs déclarés, élagués. */
export function configPayload(
  fields: readonly PluginConfigField[],
  draft: Readonly<Record<string, string>>,
): Record<string, string> {
  const payload: Record<string, string> = {};
  for (const field of fields) {
    payload[field.key] = (draft[field.key] ?? "").trim();
  }
  return payload;
}

/** Vrai si un brouillon diffère de la configuration enregistrée. */
export function configChanged(
  fields: readonly PluginConfigField[],
  draft: Readonly<Record<string, string>>,
): boolean {
  return fields.some(
    (field) => (draft[field.key] ?? "") !== field.value,
  );
}

/** Résumé d'un Secret : jamais de valeur, seulement l'état et la date. */
export function secretSummary(
  secret: PluginSecretField,
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

/** Vrai si la valeur ressemble à une URL `http`/`https` avec un hôte. */
export function isHttpUrl(value: string): boolean {
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
