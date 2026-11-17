/**
 * Modèle d'erreur unique de la Console.
 *
 * Le serveur ne renvoie qu'un **code stable** et un message technique : jamais
 * ce message n'est affiché. Toute traduction française vit ici, et une seule
 * fonction (`describeError`) sert les écrans, les formulaires et les bannières.
 */

/** Erreurs par champ : nom du champ → code stable, jamais un message brut. */
export type FieldErrors = Readonly<Record<string, string>>;

/** Options d'une erreur d'API, toutes facultatives. */
export interface ApiErrorOptions {
  retryAfter?: number | null;
  fields?: FieldErrors;
}

/**
 * Échec d'un appel à l'API, qu'il vienne du serveur ou du transport.
 *
 * `status` vaut `0` pour une panne réseau : les écrans n'ont ainsi qu'un seul
 * type d'échec à traiter.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfter: number | null;
  readonly fields: FieldErrors;

  constructor(status: number, code: string, options: ApiErrorOptions = {}) {
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = options.retryAfter ?? null;
    this.fields = options.fields ?? {};
  }

  /** Construit l'erreur depuis le corps JSON de l'API, sans jamais jeter. */
  static from(status: number, body: unknown): ApiError {
    const record = asRecord(body);
    const code =
      typeof record.error === "string" && record.error
        ? record.error
        : "http_error";
    const retryAfter =
      typeof record.retry_after === "number" &&
      Number.isFinite(record.retry_after)
        ? record.retry_after
        : null;
    return new ApiError(status, code, {
      retryAfter,
      fields: readFieldErrors(record.fields),
    });
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function readFieldErrors(value: unknown): FieldErrors {
  const record = asRecord(value);
  const fields: Record<string, string> = {};
  for (const [name, code] of Object.entries(record)) {
    if (typeof code === "string" && code) {
      fields[name] = code;
    }
  }
  return fields;
}

/**
 * Une phrase française par code stable.
 *
 * Le code `http_error` dépend du statut : une panne serveur (5xx) n'est pas un
 * refus métier (4xx). Les codes inconnus — une version future du serveur — ne
 * fuitent jamais leur nom dans l'interface.
 */
const MESSAGES: Readonly<Record<string, string>> = {
  unauthorized: "Votre Ticket Console a expiré. Reconnectez-vous.",
  setup_required:
    "L'instance n'est pas encore installée : terminez l'installation pour continuer.",
  invalid_credentials: "Identifiants incorrects.",
  already_installed: "Un Compte existe déjà : connectez-vous.",
  invalid_password: "Le mot de passe actuel est incorrect.",
  weak_password: "Le mot de passe est trop court : choisissez-en un plus long.",
  invalid_username: "Le nom d'utilisateur ne peut pas être vide.",
  too_many_attempts: "Trop de tentatives : réessayez dans quelques minutes.",
  origin_refused:
    "Origine de la requête refusée : rouvrez la Console depuis son adresse habituelle.",
  invalid_request: "La demande est incomplète ou mal formée.",
  auth_disabled: "L'authentification n'est pas configurée sur cette instance.",
  invalid_token_name: "Le nom du Jeton d'accès ne peut pas être vide.",
  invalid_kind: "La nature du Jeton doit être « client » ou « source ».",
  invalid_source:
    "Une source est obligatoire pour un Jeton « source » et interdite pour un Jeton « client ».",
  token_not_found: "Ce Jeton d'accès n'existe pas.",
  installation_incomplete:
    "L'Installation n'est pas terminée : enregistrez une clé Gemini dans les Réglages pour qu'Athena puisse parler.",
  invalid_secret:
    "Ce Secret a été refusé : vérifiez sa valeur, puis réessayez.",
  invalid_secret_name:
    "Le nom d'un Secret doit être une variable en majuscules.",
  secret_not_found: "Ce Secret n'existe pas ou n'est plus enregistré.",
  secrets_unavailable:
    "Le magasin de Secrets n'est pas disponible sur cette instance.",
  not_implemented:
    "Cette fonction n'est pas encore disponible sur cette instance.",
  not_found: "Cette ressource n'existe pas ou plus.",
  plugin_not_found: "Ce Plugin n'existe pas dans le répertoire des Plugins.",
  plugin_manifest_invalid:
    "Le manifeste de ce Plugin est illisible : corrigez-le sur le serveur.",
  invalid_plugin_config: "La configuration du Plugin est refusée.",
  invalid_plugin_secret: "Le Secret du Plugin est refusé.",
  unknown_secret: "Ce Plugin ne déclare pas ce Secret.",
  plugins_unavailable:
    "La gestion des Plugins n'est pas disponible sur cette instance.",
  plugins_state_invalid:
    "Le fichier d'état des Plugins est illisible : corrigez-le sur le serveur.",
  // Codes par champ des formulaires (Plugin, Réglages, Secrets).
  required: "Ce champ est obligatoire.",
  invalid_url: "Attendu : une URL http(s) complète.",
  unknown_field: "Ce champ n'est pas déclaré par le manifeste du Plugin.",
  invalid_value: "Cette valeur n'est pas valide.",
  too_long: "Cette valeur est trop longue.",
  invalid_response: "La réponse du serveur est illisible.",
  network:
    "Impossible de joindre le serveur. Vérifiez qu'il est démarré, puis réessayez.",
};

const UNEXPECTED =
  "Une erreur inattendue est survenue. Réessayez ; si cela persiste, consultez le journal du serveur.";

const SERVER_FAILURE =
  "Le serveur a rencontré un problème. Réessayez dans un instant.";

const REQUEST_REFUSED =
  "Le serveur a refusé la demande. Réessayez ; si cela persiste, consultez le journal du serveur.";

function messageForCode(code: string): string {
  // Le repli JSON du serveur utilise « not found » ; on accepte les deux
  // écritures sans exposer l'espace dans l'interface.
  return MESSAGES[code] ?? MESSAGES[code.replaceAll(" ", "_")] ?? UNEXPECTED;
}

/** Phrase française d'une erreur, sans jamais afficher son code. */
export function describeError(error: unknown): string {
  if (!isApiError(error)) {
    return UNEXPECTED;
  }
  if (error.code === "http_error") {
    return error.status >= 500 ? SERVER_FAILURE : REQUEST_REFUSED;
  }
  if (error.code === "too_many_attempts" && error.retryAfter !== null) {
    const seconds = Math.max(1, Math.ceil(error.retryAfter));
    return `Trop de tentatives : réessayez dans ${seconds} s.`;
  }
  return messageForCode(error.code);
}

/** Phrase française de chaque erreur par champ, prête pour un formulaire. */
export function describeFieldErrors(error: unknown): Record<string, string> {
  if (!isApiError(error)) {
    return {};
  }
  const messages: Record<string, string> = {};
  for (const [name, code] of Object.entries(error.fields)) {
    messages[name] = messageForCode(code);
  }
  return messages;
}

/** Vrai pour une erreur d'API, y compris une panne de transport. */
export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

/** Statut HTTP d'une erreur d'API, ou `null` si ce n'en est pas une. */
export function errorStatus(error: unknown): number | null {
  return isApiError(error) ? error.status : null;
}

/** Code stable d'une erreur d'API, ou chaîne vide. */
export function errorCode(error: unknown): string {
  return isApiError(error) ? error.code : "";
}

/** Vrai pour le seul refus qui ramène à l'écran de connexion. */
export function isUnauthorized(error: unknown): boolean {
  return isApiError(error) && error.status === 401;
}
