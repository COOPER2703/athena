/**
 * Panneau Compte : validation pure du changement de mot de passe.
 *
 * Module **pur** : aucun DOM, aucun `fetch`. La longueur minimale vient de
 * `GET /api/session` et n'est jamais recopiée en constante ; le serveur reste
 * l'autorité, ce module évite seulement l'aller-retour évident.
 */

import { describeError, describeFieldErrors, errorCode } from "../../api/errors";

/** Saisie du formulaire de changement de mot de passe. */
export interface PasswordDraft {
  current: string;
  next: string;
  confirmation: string;
}

/**
 * Valide la saisie avant l'appel.
 *
 * Rend un message français par champ ; `minLength` nul laisse le serveur juger
 * la longueur, sans inventer de règle côté navigateur.
 */
export function validatePasswordChange(
  draft: PasswordDraft,
  minLength: number | null,
): Record<string, string> {
  const fields: Record<string, string> = {};
  if (draft.current === "") {
    fields.current_password = "Saisissez le mot de passe actuel.";
  }
  if (draft.next === "") {
    fields.new_password = "Saisissez le nouveau mot de passe.";
  } else if (minLength !== null && draft.next.length < minLength) {
    fields.new_password = `Le mot de passe doit contenir au moins ${minLength} caractères.`;
  }
  if (draft.confirmation !== draft.next) {
    fields.confirmation = "Les deux mots de passe ne correspondent pas.";
  }
  return fields;
}

/**
 * Range un refus du serveur sous le bon champ.
 *
 * Les codes stables du changement de mot de passe ne portent pas de `fields` :
 * un mot de passe actuel faux vise `current_password`, un mot de passe faible
 * vise `new_password`. Le reste retombe sur le message global.
 */
export function passwordErrorFields(
  error: unknown,
): Record<string, string> {
  const fields = describeFieldErrors(error);
  const code = errorCode(error);
  if (code === "invalid_password") {
    fields.current_password = describeError(error);
  } else if (code === "weak_password") {
    fields.new_password = describeError(error);
  }
  return fields;
}

/** Phrase globale d'un échec, ou `null` quand un champ la porte déjà. */
export function passwordErrorMessage(error: unknown): string | null {
  return Object.keys(passwordErrorFields(error)).length === 0
    ? describeError(error)
    : null;
}
