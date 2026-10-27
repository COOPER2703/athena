/** Accès à l'API JSON de la Console. */

import { normalizeAuth, type AuthSession } from "./auth";
import { normalizeStatus, type StatusData } from "./status";

/** Signature minimale de `fetch`, injectable dans les tests. */
export type Fetcher = (input: string, init: RequestInit) => Promise<Response>;

/** Erreur d'API porteuse du statut, du code et du délai de réessai. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfter: number | null;

  constructor(
    status: number,
    code: string,
    message: string,
    retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }

  /** Construit l'erreur depuis le corps JSON de l'API, sans jamais jeter. */
  static from(status: number, body: unknown): ApiError {
    const record =
      typeof body === "object" && body !== null
        ? (body as Record<string, unknown>)
        : {};
    const code = typeof record.error === "string" ? record.error : "http_error";
    const message =
      typeof record.message === "string"
        ? record.message
        : `Le serveur a répondu ${status}.`;
    const retryAfter =
      typeof record.retry_after === "number" ? record.retry_after : null;
    return new ApiError(status, code, message, retryAfter);
  }
}

/**
 * Appel JSON de même origine : le cookie du Ticket Console part seul.
 *
 * Une réponse non-2xx devient une `ApiError` ; une panne réseau devient une
 * `ApiError` de statut `0`, pour que les écrans n'aient qu'un type d'échec à
 * traiter.
 */
export async function requestJson(
  path: string,
  init: RequestInit = {},
  fetcher: Fetcher = fetch,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetcher(path, {
      ...init,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(init.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(0, "network", "Impossible de joindre l'API de la Console.");
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw ApiError.from(response.status, body);
  }
  return body;
}

/** Réponse de `GET /api/status`, enveloppe versionnée comprise. */
export interface StatusPayload extends StatusData {
  v: number;
}

/** Lit l'instantané courant ; un 401 est une `ApiError` reconnaissable. */
export async function fetchStatus(
  signal?: AbortSignal,
  fetcher: Fetcher = fetch,
): Promise<StatusPayload> {
  const body = await requestJson("/api/status", { signal }, fetcher);
  const version =
    typeof body === "object" && body !== null && "v" in body
      ? Number((body as { v?: unknown }).v)
      : NaN;
  return {
    v: Number.isFinite(version) ? version : 0,
    ...normalizeStatus(body),
  };
}

/** Lit l'état de session : installation requise, connecté, nom du Compte. */
export async function fetchSession(
  signal?: AbortSignal,
  fetcher: Fetcher = fetch,
): Promise<AuthSession> {
  return normalizeAuth(await requestJson("/api/session", { signal }, fetcher));
}

/** Formulaire d'installation : Code d'installation, nom d'utilisateur, mot de passe. */
export interface SetupDraft {
  code: string;
  username: string;
  password: string;
}

/** Identifiants de connexion. */
export interface Credentials {
  username: string;
  password: string;
}

/** Crée l'unique Compte et ouvre un Ticket Console. */
export async function setupAccount(
  draft: SetupDraft,
  fetcher: Fetcher = fetch,
): Promise<AuthSession> {
  const body = await requestJson(
    "/api/setup",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    },
    fetcher,
  );
  return normalizeAuth(body);
}

/** Vérifie les identifiants et ouvre un Ticket Console. */
export async function login(
  credentials: Credentials,
  fetcher: Fetcher = fetch,
): Promise<AuthSession> {
  const body = await requestJson(
    "/api/login",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credentials),
    },
    fetcher,
  );
  return normalizeAuth(body);
}
