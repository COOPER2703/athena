/** Accès à l'API JSON de la Console, toujours en même origine. */

import {
  normalizeAuth,
  sessionFromLogin,
  type AuthSession,
} from "../auth/session";
import { ApiError } from "./errors";

/** Signature minimale de `fetch`, injectable dans les tests. */
export type Fetcher = (input: string, init: RequestInit) => Promise<Response>;

/**
 * Appel JSON de même origine : le cookie du Ticket Console part seul.
 *
 * Une réponse non-2xx devient une `ApiError` ; une panne réseau devient une
 * `ApiError` de statut `0`, pour que les écrans n'aient qu'un type d'échec à
 * traiter. Aucun message du serveur n'est conservé : seul son code stable sert
 * à composer la phrase affichée (`errors.ts`).
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
    throw new ApiError(0, "network");
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

/** Corps JSON prêt à envoyer, avec l'en-tête qui va bien. */
export function jsonBody(
  value: unknown,
): Pick<RequestInit, "body" | "headers"> {
  return {
    body: JSON.stringify(value),
    headers: { "Content-Type": "application/json" },
  };
}

/** Lit l'état d'identité et d'installation. */
export async function fetchSession(fetcher?: Fetcher): Promise<AuthSession> {
  return normalizeAuth(await requestJson("/api/session", {}, fetcher));
}

/** Identifiants de connexion. */
export interface Credentials {
  username: string;
  password: string;
}

/** Vérifie les identifiants et ouvre un Ticket Console. */
export async function login(
  credentials: Credentials,
  fetcher?: Fetcher,
): Promise<AuthSession> {
  const body = await requestJson(
    "/api/login",
    { method: "POST", ...jsonBody(credentials) },
    fetcher,
  );
  return sessionFromLogin(body);
}

/** Révoque le Ticket Console courant ; le serveur efface le cookie. */
export async function logout(fetcher?: Fetcher): Promise<void> {
  await requestJson("/api/logout", { method: "POST" }, fetcher);
}
