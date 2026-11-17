/**
 * État d'identité et d'Installation de la Console, déduit de `GET /api/session`.
 *
 * Module **pur** : aucun DOM, aucun `fetch`. Le vocabulaire suit le glossaire —
 * **Compte** (identité d'accès), **Ticket Console** (état du navigateur) et
 * **Installation** (Compte créé **et** clé Gemini utilisable), à ne pas
 * confondre avec la **Session** Live du noyau.
 */

/** Réponse normalisée de `GET /api/session`. */
export interface AuthSession {
  /** Vrai tant qu'aucun Compte n'existe : l'écran d'installation s'impose. */
  setupRequired: boolean;
  /** Vrai quand le Ticket Console du navigateur est valide. */
  authenticated: boolean;
  /** Nom du Compte connecté, sinon `null`. */
  username: string | null;
  /** Vrai quand une clé Gemini utilisable existe : Athena peut parler. */
  geminiConfigured: boolean;
  /** Vrai quand le Compte existe **et** qu'une clé Gemini utilisable est là. */
  installationComplete: boolean;
  /** Vrai quand la clé enregistrée n'est pas celle chargée au démarrage. */
  restartRequired: boolean;
  /** Vrai quand la clé maître ne déchiffre plus le magasin de Secrets. */
  secretsUnreadable: boolean;
  /**
   * Longueur minimale du mot de passe, telle que le serveur l'applique.
   * `null` quand la réponse ne la porte pas : le front n'invente alors aucune
   * règle et laisse le serveur refuser.
   */
  passwordMinLength: number | null;
}

/** Chaîne non vide, ou `null`. */
function readText(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

/** Entier strictement positif, ou `null` : jamais une politique inventée. */
function readLength(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : null;
}

/** Normalise une réponse inconnue : tout champ absent ou mal typé est sûr. */
export function normalizeAuth(body: unknown): AuthSession {
  const record =
    typeof body === "object" && body !== null
      ? (body as Record<string, unknown>)
      : {};
  return {
    setupRequired: record.setup_required === true,
    authenticated: record.authenticated === true,
    username: readText(record.username),
    geminiConfigured: record.gemini_configured === true,
    installationComplete: record.installation_complete === true,
    restartRequired: record.restart_required === true,
    secretsUnreadable: record.secrets_unreadable === true,
    passwordMinLength: readLength(record.password_min_length),
  };
}

/** Session d'un visiteur non authentifié sur une instance installée. */
export const ANONYMOUS_SESSION: AuthSession = {
  setupRequired: false,
  authenticated: false,
  username: null,
  geminiConfigured: false,
  installationComplete: false,
  restartRequired: false,
  secretsUnreadable: false,
  passwordMinLength: null,
};

/**
 * Construit l'identité à partir d'une réponse de connexion.
 *
 * `POST /api/login` n'ouvre un Ticket que sur une instance installée : il ne
 * peut donc pas exiger l'installation. Il ne décrit pas non plus l'état de la
 * clé Gemini ; l'appelant relit `GET /api/session` avant d'afficher quoi que ce
 * soit, et d'ici là rien ne prétend que l'Installation est terminée.
 */
export function sessionFromLogin(body: unknown): AuthSession {
  const session = normalizeAuth(body);
  return { ...session, setupRequired: false };
}

/** Résultat de `POST /api/setup` : l'identité nouvelle et la clé maître. */
export interface SetupOutcome {
  session: AuthSession;
  /**
   * Clé maître rendue **une seule fois**, à la création. `null` quand un
   * déploiement en avait déjà fourni une : il n'y a alors rien à noter.
   */
  masterKey: string | null;
}

/**
 * Construit l'identité et la clé maître d'une réponse d'Installation.
 *
 * Aucune valeur n'est conservée ici ni ailleurs : la clé maître ne vit que
 * dans l'état mémoire de l'écran qui l'affiche, jamais dans un stockage ni
 * dans l'URL.
 */
export function sessionFromSetup(body: unknown): SetupOutcome {
  const record =
    typeof body === "object" && body !== null
      ? (body as Record<string, unknown>)
      : {};
  return {
    session: { ...normalizeAuth(body), setupRequired: false },
    masterKey: readText(record.master_key),
  };
}
