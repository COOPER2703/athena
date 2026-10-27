/**
 * Panneau Compte de la Console : changement de mot de passe et déconnexion.
 *
 * Module **pur** : aucun DOM, aucun `fetch`. Il ne manipule que les valeurs du
 * formulaire et les erreurs de l'API, pour que les règles (mot de passe actuel
 * obligatoire, longueur minimale, confirmation) soient testables sans écran.
 */

import { UNAUTHORIZED_MESSAGE, errorStatus } from "./auth";

/** Longueur minimale affichée ; le serveur reste l'autorité (400 explicite). */
export const MIN_PASSWORD_LENGTH = 12;

/** Message du retour au login après « déconnecter partout ». */
export const LOGOUT_ALL_MESSAGE =
  "Vous avez été déconnecté de tous vos navigateurs.";

const NETWORK_MESSAGE = "Impossible de joindre l'API de la Console.";

/** Brouillon du formulaire de changement de mot de passe. */
export interface PasswordChangeDraft {
  current: string;
  next: string;
  confirmation: string;
}

/** Charge utile envoyée à `POST /api/password`. */
export interface PasswordChangePayload {
  current_password: string;
  new_password: string;
}

/**
 * Valide le formulaire avant l'appel : le serveur revérifie tout.
 *
 * Le mot de passe actuel est exigé même si le navigateur possède déjà un
 * Ticket Console : c'est la preuve demandée pour durcir le Compte.
 */
export function validatePasswordChange(
  draft: PasswordChangeDraft,
): string | null {
  if (draft.current.length === 0) {
    return "Saisissez votre mot de passe actuel.";
  }
  if (draft.next.length < MIN_PASSWORD_LENGTH) {
    return `Le nouveau mot de passe doit contenir au moins ${MIN_PASSWORD_LENGTH} caractères.`;
  }
  if (draft.next !== draft.confirmation) {
    return "Les deux mots de passe ne correspondent pas.";
  }
  return null;
}

/** Traduit le brouillon en contrat JSON, sans jamais envoyer la confirmation. */
export function passwordChangePayload(
  draft: PasswordChangeDraft,
): PasswordChangePayload {
  return {
    current_password: draft.current,
    new_password: draft.next,
  };
}

function errorCode(error: unknown): string {
  if (typeof error !== "object" || error === null) {
    return "";
  }
  const code = (error as Record<string, unknown>).code;
  return typeof code === "string" ? code : "";
}

/**
 * Message lisible d'un échec de changement de mot de passe.
 *
 * Un mot de passe actuel faux est un `403` : la requête reste authentifiée, on
 * ne renvoie donc pas l'utilisateur au login. Seul un `401` (Ticket révoqué)
 * ramène à l'écran de connexion.
 */
export function passwordChangeErrorMessage(error: unknown): string {
  const status = errorStatus(error);
  if (status === 401) {
    return UNAUTHORIZED_MESSAGE;
  }
  if (status === null || status === 0) {
    return NETWORK_MESSAGE;
  }
  if (status === 403 && errorCode(error) === "invalid_password") {
    return "Le mot de passe actuel est incorrect.";
  }
  if (status === 400 && errorCode(error) === "weak_password") {
    return `Le nouveau mot de passe doit contenir au moins ${MIN_PASSWORD_LENGTH} caractères.`;
  }
  return "Le changement de mot de passe a échoué.";
}
