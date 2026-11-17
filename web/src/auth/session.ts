/**
 * État d'identité de la Console, déduit de `GET /api/session`.
 *
 * Module **pur** : aucun DOM, aucun `fetch`. Le vocabulaire suit le glossaire —
 * **Compte** (identité d'accès) et **Ticket Console** (état du navigateur), à ne
 * pas confondre avec la **Session** Live du noyau.
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

/** Session d'un visiteur non authentifié sur une instance installée. */
export const ANONYMOUS_SESSION: AuthSession = {
  setupRequired: false,
  authenticated: false,
  username: null,
};

/**
 * Construit l'identité à partir d'une réponse de connexion.
 *
 * `POST /api/login` ne renvoie pas `setup_required` : un Ticket qui vient
 * d'être ouvert sur une instance installée ne peut pas exiger l'installation.
 */
export function sessionFromLogin(body: unknown): AuthSession {
  const session = normalizeAuth(body);
  return { ...session, setupRequired: false };
}
