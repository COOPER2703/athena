/**
 * Jetons d'accès : liste, création et révocation.
 *
 * Un Jeton est le credential d'une machine — `client` pour le transport des
 * Clients, `source` pour le Webhook — à ne pas confondre avec le **Ticket
 * Console** du navigateur. Aucune réponse de liste ne porte de secret : seul
 * `POST /api/tokens` rend le secret, une seule fois, à l'appelant qui l'affiche.
 */

import { jsonBody, requestJson, type Fetcher } from "./client";
import { ApiError } from "./errors";

/** Nature d'un Jeton : le transport des Clients, ou une source du Webhook. */
export type TokenKind = "client" | "source";

/** Jeton tel que `GET /api/tokens` le décrit — jamais de secret ni d'empreinte. */
export interface AccessToken {
  id: string;
  name: string;
  kind: TokenKind;
  /** Source du Webhook, `null` pour un Jeton de transport. */
  source: string | null;
  /** Préfixe affichable (`ath_…`), jamais le secret complet. */
  prefix: string;
  /** Date de création, en secondes epoch, ou `null` si illisible. */
  createdAt: number | null;
  /** Dernière utilisation connue, en secondes epoch. */
  lastUsedAt: number | null;
  /** Date de révocation ; `null` tant que le Jeton est actif. */
  revokedAt: number | null;
}

/** Charge utile de création, validée par le formulaire. */
export interface TokenDraftValue {
  name: string;
  kind: TokenKind;
  source: string | null;
}

/** Réponse de `POST /api/tokens` : le secret n'apparaît qu'ici, une fois. */
export interface IssuedToken {
  token: AccessToken;
  secret: string;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function seconds(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isKind(value: unknown): value is TokenKind {
  return value === "client" || value === "source";
}

/** Normalise un Jeton ; `null` si l'entrée est inutilisable. */
function normalizeToken(body: unknown): AccessToken | null {
  const entry = asRecord(body);
  const id = text(entry.id);
  const name = text(entry.name);
  if (!id || !name || !isKind(entry.kind)) {
    return null;
  }
  const source = text(entry.source);
  return {
    id,
    name,
    kind: entry.kind,
    source: entry.kind === "source" && source ? source : null,
    prefix: text(entry.prefix),
    createdAt: seconds(entry.created_at),
    lastUsedAt: seconds(entry.last_used_at),
    revokedAt: seconds(entry.revoked_at),
  };
}

/** Lit la liste des Jetons ; une réponse malformée rend une liste vide. */
export function normalizeTokens(body: unknown): AccessToken[] {
  const tokens = asRecord(body).tokens;
  if (!Array.isArray(tokens)) {
    return [];
  }
  return tokens.flatMap((entry) => {
    const token = normalizeToken(entry);
    return token === null ? [] : [token];
  });
}

/** Lit la réponse de création, ou `null` si le secret manque. */
export function normalizeIssuedToken(body: unknown): IssuedToken | null {
  const entry = asRecord(body);
  const token = normalizeToken(entry.token);
  const secret = text(entry.secret).trim();
  if (token === null || !secret) {
    return null;
  }
  return { token, secret };
}

/** Liste les Jetons d'accès ; le contrat ne contient jamais de secret. */
export async function fetchTokens(
  fetcher?: Fetcher,
): Promise<AccessToken[]> {
  return normalizeTokens(await requestJson("/api/tokens", {}, fetcher));
}

/** Crée un Jeton et rend son secret en clair — une seule fois. */
export async function createAccessToken(
  draft: TokenDraftValue,
  fetcher?: Fetcher,
): Promise<IssuedToken> {
  const body = await requestJson(
    "/api/tokens",
    { method: "POST", ...jsonBody(draft) },
    fetcher,
  );
  const issued = normalizeIssuedToken(body);
  if (issued === null) {
    throw new ApiError(0, "invalid_response");
  }
  return issued;
}

/** Révoque un Jeton individuellement ; la révocation prend effet aussitôt. */
export async function revokeAccessToken(
  id: string,
  fetcher?: Fetcher,
): Promise<void> {
  await requestJson(
    `/api/tokens/${encodeURIComponent(id)}`,
    { method: "DELETE" },
    fetcher,
  );
}
