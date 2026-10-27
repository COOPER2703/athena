/**
 * État d'authentification de la Console, déduit de `GET /api/session`.
 *
 * Module **pur** : aucun DOM, aucun `fetch`. Le vocabulaire suit le glossaire —
 * **Compte** (identité d'accès) et **Ticket Console** (état du navigateur), à
 * ne pas confondre avec la **Session** Live du noyau (`session.ts`).
 */

/** Réponse normalisée de `GET /api/session`. */
export interface AuthSession {
  /** Vrai tant qu'aucun Compte n'existe : l'écran d'installation s'impose. */
  setupRequired: boolean;
  /** Vrai quand le Ticket Console du navigateur est valide. */
  authenticated: boolean;
  /** Nom du Compte connecté, sinon `null`. */
  username: string | null;
}

/** Écran à afficher, choisi uniquement à partir de l'état de session. */
export type AuthScreen = "setup" | "login" | "console";

/** Message affiché quand un Ticket Console a expiré ou été révoqué. */
export const UNAUTHORIZED_MESSAGE =
  "Votre Ticket Console a expiré. Reconnectez-vous.";

const NETWORK_MESSAGE = "Impossible de joindre l'API de la Console.";

/** Normalise une réponse inconnue : tout champ absent ou mal typé est sûr. */
export function normalizeAuth(body: unknown): AuthSession {
  const record =
    typeof body === "object" && body !== null
      ? (body as Record<string, unknown>)
      : {};
  const username = record.username;
  return {
    setupRequired: record.setup_required === true,
    authenticated: record.authenticated === true,
    username: typeof username === "string" && username ? username : null,
  };
}

/**
 * L'installation prime : tant qu'aucun Compte n'existe, seule la création du
 * Compte est possible. Ensuite, un Ticket valide ouvre la console, sinon la
 * connexion.
 */
export function screenForAuth(session: AuthSession): AuthScreen {
  if (session.setupRequired) {
    return "setup";
  }
  if (session.authenticated) {
    return "console";
  }
  return "login";
}

/** Statut HTTP porté par une erreur d'API, ou `null` si ce n'en est pas une. */
export function errorStatus(error: unknown): number | null {
  if (typeof error !== "object" || error === null) {
    return null;
  }
  const record = error as Record<string, unknown>;
  return typeof record.status === "number" ? record.status : null;
}

/** Vrai pour un refus d'authentification : le seul cas qui ramène au login. */
export function isUnauthorized(error: unknown): boolean {
  return errorStatus(error) === 401;
}

function errorCode(error: unknown): string {
  if (typeof error !== "object" || error === null) {
    return "";
  }
  const code = (error as Record<string, unknown>).code;
  return typeof code === "string" ? code : "";
}

function errorMessage(error: unknown): string | null {
  if (typeof error !== "object" || error === null) {
    return null;
  }
  const message = (error as Record<string, unknown>).message;
  return typeof message === "string" && message ? message : null;
}

function retryAfterSeconds(error: unknown): number | null {
  if (typeof error !== "object" || error === null) {
    return null;
  }
  const retry = (error as Record<string, unknown>).retryAfter;
  return typeof retry === "number" && Number.isFinite(retry) ? retry : null;
}

/**
 * Message lisible d'un échec d'installation ou de connexion.
 *
 * Un échec d'identifiants est **non discriminant** : le serveur renvoie le même
 * code pour un utilisateur inconnu et un mot de passe faux, et l'écran ne dit
 * pas lequel est en cause.
 */
export function authErrorMessage(error: unknown): string {
  const status = errorStatus(error);
  if (status === null) {
    return NETWORK_MESSAGE;
  }
  if (status === 0) {
    return NETWORK_MESSAGE;
  }
  if (status === 401) {
    return "Identifiants incorrects.";
  }
  if (status === 429) {
    const seconds = retryAfterSeconds(error);
    return seconds === null
      ? "Trop de tentatives. Réessayez dans quelques minutes."
      : `Trop de tentatives. Réessayez dans ${seconds} s.`;
  }
  if (status === 409 || errorCode(error) === "already_installed") {
    return "Un Compte existe déjà : connectez-vous.";
  }
  if (status === 403 && errorCode(error) === "invalid_code") {
    return "Code d'installation invalide ou déjà utilisé.";
  }
  return errorMessage(error) ?? `Le serveur a répondu ${status}.`;
}
