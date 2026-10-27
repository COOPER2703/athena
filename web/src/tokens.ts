/**
 * Jetons d'accès : normalisation et présentation, sans navigateur.
 *
 * Module **pur** : aucun DOM, aucun `fetch`, aucun stockage. Le secret rendu à
 * la création ne vit que dans l'état local du panneau (`TokenPanel`) ; rien ici
 * ne le conserve ni ne le relit.
 *
 * Le vocabulaire suit le glossaire : un **Jeton d'accès** est le credential
 * d'une machine (`client` pour le transport, `source` pour le Webhook), à ne pas
 * confondre avec le **Ticket Console** du navigateur.
 */

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

/** Réponse de `POST /api/tokens` : le secret n'apparaît qu'ici, une fois. */
export interface IssuedToken {
  token: AccessToken;
  secret: string;
}

/** Saisie du formulaire de création. */
export interface TokenDraft {
  name: string;
  kind: TokenKind;
  source: string;
}

/** Saisie validée, prête pour `POST /api/tokens`. */
export interface TokenDraftValue {
  name: string;
  kind: TokenKind;
  source: string | null;
}

export type TokenDraftCheck =
  | { ok: true; value: TokenDraftValue }
  | { ok: false; message: string };

/** Avertissement affiché avec le secret : il ne sera plus jamais relisible. */
export const SECRET_ONCE_WARNING =
  "Copiez ce secret maintenant : il ne sera plus jamais affiché.";

function record(body: unknown): Record<string, unknown> {  return typeof body === "object" && body !== null
    ? (body as Record<string, unknown>)
    : {};
}

function seconds(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isKind(value: unknown): value is TokenKind {
  return value === "client" || value === "source";
}

/** Normalise un Jeton ; `null` si l'entrée est inutilisable. */
function normalizeToken(body: unknown): AccessToken | null {
  const entry = record(body);
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
  const tokens = record(body).tokens;
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
  const entry = record(body);
  const token = normalizeToken(entry.token);
  const secret = text(entry.secret).trim();
  if (token === null || !secret) {
    return null;
  }
  return { token, secret };
}

/** Nature ou source d'un Jeton, telle que la liste la montre. */
export function tokenTargetLabel(token: AccessToken): string {
  if (token.kind === "client") {
    return "Transport client";
  }
  return token.source ? `Source « ${token.source} »` : "Source";
}

/** État lisible d'un Jeton : la révocation reste visible après coup. */
export function tokenStatusLabel(token: AccessToken): string {
  return token.revokedAt === null ? "Actif" : "Révoqué";
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

/** Date courte en heure locale (`jj/mm/aaaa hh:mm`), ou un tiret si absente. */
export function formatTokenDate(secondsValue: number | null): string {
  if (secondsValue === null || !Number.isFinite(secondsValue)) {
    return "—";
  }
  const date = new Date(secondsValue * 1000);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return `${pad(date.getDate())}/${pad(date.getMonth() + 1)}/${date.getFullYear()} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** Dernière utilisation : un Jeton jamais présenté le dit explicitement. */
export function formatLastUsed(secondsValue: number | null): string {
  return secondsValue === null ? "Jamais utilisé" : formatTokenDate(secondsValue);
}

/**
 * Valide la saisie de création comme le serveur la jugera.
 *
 * Le contrôle local évite un aller-retour évident ; le serveur reste
 * l'autorité (`invalid_source`, `invalid_token_name`).
 */
export function validateTokenDraft(draft: TokenDraft): TokenDraftCheck {
  const name = draft.name.trim();
  if (!name) {
    return { ok: false, message: "Le nom du Jeton ne peut pas être vide." };
  }
  if (draft.kind === "source") {
    const source = draft.source.trim();
    if (!source) {
      return {
        ok: false,
        message: "Une source est obligatoire pour un Jeton source.",
      };
    }
    return { ok: true, value: { name, kind: "source", source } };
  }
  return { ok: true, value: { name, kind: "client", source: null } };
}
