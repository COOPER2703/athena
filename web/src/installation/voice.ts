/**
 * Étape « voix et profil » de l'Installation : dérivation pure.
 *
 * Les valeurs viennent de `GET /api/settings`, jamais d'une constante du front.
 * Le brouillon part des champs **écrivables** et le `PUT` porte l'objet complet
 * exigé par le serveur ; seuls la voix et les modèles sont exposés ici.
 * Module pur : aucun DOM, aucun `fetch`.
 */

import type { SettingField } from "../api/settings";
import {
  baselineValue,
  draftFromFields,
  isWritable,
  settingsPayload,
  settingLabel,
  validateSettings,
} from "../modules/settings/model";

/** Champs de l'étape, dans l'ordre d'affichage. */
export const VOICE_KEYS = ["voice", "live_model", "judge_model"] as const;

/** Un champ de l'étape, avec son libellé français. */
export interface VoiceRow {
  key: string;
  label: string;
  value: string;
}

/** Champs de l'étape réellement écrivables sur cette instance. */
export function voiceFields(
  fields: readonly SettingField[],
): SettingField[] {
  return VOICE_KEYS.flatMap((key) => {
    const field = fields.find((item) => item.key === key);
    return field !== undefined && isWritable(field) ? [field] : [];
  });
}

/** Lignes affichées : valeur effective, ou valeur en attente si elle prime. */
export function voiceRows(
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
): VoiceRow[] {
  return voiceFields(fields).map((field) => ({
    key: field.key,
    label: settingLabel(field.key),
    value: draft[field.key] ?? baselineValue(field),
  }));
}

/** Brouillon initial : tous les champs écrivables, valeurs du serveur. */
export function voiceDraft(
  fields: readonly SettingField[],
): Record<string, string> {
  return draftFromFields(fields);
}

/** Erreurs locales du brouillon complet, en miroir du serveur. */
export function voiceErrors(
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
): Record<string, string> {
  return validateSettings(fields, draft);
}

/**
 * Objet **complet** envoyé par `PUT /api/settings`.
 *
 * Les champs hors étape gardent leur valeur effective : aucun changement
 * d'hôte ou de port n'est introduit, donc `confirm_url` n'est jamais requis.
 */
export function voicePayload(
  fields: readonly SettingField[],
  draft: Readonly<Record<string, string>>,
): Record<string, string> {
  return settingsPayload(fields, draft);
}
