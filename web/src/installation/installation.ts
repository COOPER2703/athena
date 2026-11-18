/**
 * Parcours d'Installation : étapes, validation et état manquant.
 *
 * Module **pur** : il ne dépend ni du DOM ni de React. L'écran guidé et la
 * bannière permanente lisent les mêmes fonctions, pour qu'une seule règle dise
 * quand l'Installation est finie et ce qu'il reste à faire (ADR-0026).
 */

import { describeError, describeFieldErrors } from "../api/errors";
import type { AuthSession } from "../auth/session";

/** Étapes numérotées du parcours, dans l'ordre. */
export const INSTALL_STEPS = ["account", "gemini", "voice", "summary"] as const;

/** Étape du parcours ; la clé maître est un interlude après le Compte. */
export type InstallStep = (typeof INSTALL_STEPS)[number] | "master-key";

/** Route du réglage à compléter : la clé Gemini, dans la zone Réglages. */
export const GEMINI_SETTINGS_PATH = "/settings#secret-GEMINI_API_KEY";

/**
 * Étape d'entrée du parcours, ou `null` quand il ne doit plus apparaître.
 *
 * Un visiteur sans Compte commence au Compte ; un propriétaire dont le Compte
 * existe déjà reprend à la clé Gemini ; une Installation terminée ne rouvre
 * jamais l'écran guidé (l'`IdentityGate` route vers l'application).
 */
export function startStep(session: AuthSession): InstallStep | null {
  if (session.setupRequired) {
    return "account";
  }
  if (!session.installationComplete) {
    return "gemini";
  }
  return null;
}

/** Étape suivante ; `null` après le récapitulatif. */
export function nextStep(step: InstallStep): InstallStep | null {
  switch (step) {
    case "account":
      return "master-key";
    case "master-key":
      return "gemini";
    case "gemini":
      return "voice";
    case "voice":
      return "summary";
    case "summary":
      return null;
  }
}

/** Libellé court d'une étape, pour le fil d'Ariane. */
export function stepLabel(step: InstallStep): string {
  switch (step) {
    case "account":
      return "Compte";
    case "master-key":
      return "Clé maître";
    case "gemini":
      return "Clé Gemini";
    case "voice":
      return "Voix et profil";
    case "summary":
      return "Récapitulatif";
  }
}

/** Position d'une étape dans le fil : 1 à 4, la clé maître comptant pour le Compte. */
export function stepPosition(step: InstallStep): number {
  if (step === "master-key") {
    return 1;
  }
  const index = INSTALL_STEPS.indexOf(step);
  return index < 0 ? 1 : index + 1;
}

/** Saisie du premier écran : nom, mot de passe et confirmation. */
export interface AccountDraft {
  username: string;
  password: string;
  confirmation: string;
}

/**
 * Valide la saisie du Compte avant tout appel serveur.
 *
 * La longueur minimale vient du serveur (`password_min_length`) et n'est
 * jamais recopiée ici : quand il ne la donne pas, seule la présence est
 * vérifiée et c'est lui qui tranche.
 */
export function validateAccount(
  draft: AccountDraft,
  minLength: number | null,
): Record<string, string> {
  const fields: Record<string, string> = {};
  if (draft.username.trim() === "") {
    fields.username = "Saisissez le nom du Compte.";
  }
  if (draft.password === "") {
    fields.password = "Saisissez un mot de passe.";
  } else if (minLength !== null && draft.password.length < minLength) {
    fields.password = `Le mot de passe doit contenir au moins ${minLength} caractères.`;
  }
  if (draft.confirmation !== draft.password) {
    fields.confirmation = "Les deux mots de passe ne correspondent pas.";
  }
  return fields;
}

/**
 * Erreurs de champ d'une création de Compte.
 *
 * Le serveur répond par un code global (`weak_password`, `invalid_username`)
 * et parfois par des champs : les deux formes atterrissent sous le bon champ,
 * jamais dans le code brut.
 */
export function accountErrorFields(error: unknown): Record<string, string> {
  const fields = describeFieldErrors(error);
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? (error as { code?: unknown }).code
      : undefined;

  if (code === "weak_password") {
    fields.password = describeError(error);
  } else if (code === "invalid_username") {
    fields.username = describeError(error);
  }
  return fields;
}

/** Phrase d'une clé Gemini refusée par le service, sinon `null`. */
export function geminiFieldError(error: unknown): string | null {
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? (error as { code?: unknown }).code
      : undefined;
  if (code !== "invalid_secret") {
    return null;
  }
  return "Cette clé Gemini a été refusée : vérifiez-la dans Google AI Studio, puis réessayez.";
}

/** Ce qui manque encore pour qu'Athena soit installée et à jour. */
export interface InstallationGap {
  id: "gemini" | "secrets" | "restart";
  title: string;
  body: string;
}

/**
 * Manques de l'Installation, dans l'ordre où ils bloquent l'usage.
 *
 * La clé Gemini vient d'abord : sans elle, aucune surface d'exécution ne
 * s'ouvre et la Conversation reste désactivée.
 */
export function installationGaps(session: AuthSession): InstallationGap[] {
  const gaps: InstallationGap[] = [];
  if (!session.geminiConfigured) {
    gaps.push({
      id: "gemini",
      title: "Clé Gemini manquante",
      body: "Athena ne peut pas encore parler : le transport des Clients, le Webhook et Gemini restent fermés, et la Conversation est désactivée.",
    });
  }
  if (session.secretsUnreadable) {
    gaps.push({
      id: "secrets",
      title: "Secrets illisibles",
      body: "La clé maître ne déchiffre plus le magasin : les Secrets doivent être ressaisis. Rien n'a été réinitialisé en silence.",
    });
  }
  if (session.restartRequired) {
    gaps.push({
      id: "restart",
      title: "Redémarrage requis",
      body: "La clé Gemini enregistrée n'est pas encore celle chargée par le processus : Athena continue avec l'ancienne jusqu'au redémarrage du serveur.",
    });
  }
  return gaps;
}

/**
 * Vrai quand la Conversation peut recevoir un Tour.
 *
 * Il faut une clé Gemini utilisable **chargée par le serveur** : la première
 * clé est activée à chaud à la fin de l'Installation. Une clé *remplacée*
 * ensuite attend un redémarrage (`restartRequired`), mais l'ancienne reste
 * chargée : la Conversation demeure utilisable et le bandeau explique l'écart.
 */
export function isConversationAvailable(
  session: AuthSession | undefined,
): boolean {
  return (
    session !== undefined &&
    session.authenticated &&
    session.installationComplete
  );
}
