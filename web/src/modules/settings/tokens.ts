/**
 * Jetons d'accès : libellés, validation de création et commande de couplage.
 *
 * Module **pur** : aucun DOM, aucun `fetch`, aucun stockage. Le secret rendu à
 * la création ne vit que dans l'état local du panneau, jamais ici.
 */

import type { AccessToken, TokenDraftValue, TokenKind } from "../../api/tokens";

/** Saisie du formulaire de création. */
export interface TokenDraft {
  name: string;
  kind: TokenKind;
  source: string;
}

export type TokenDraftCheck =
  | { ok: true; value: TokenDraftValue }
  | { ok: false; message: string };

/** Avertissement affiché avec le secret : il ne sera plus jamais relisible. */
export const SECRET_ONCE_WARNING =
  "Copiez ce secret maintenant : il ne sera plus jamais affiché.";

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

/**
 * Commande de couplage du Client desktop.
 *
 * Elle reprend les deux surcharges de processus que le Client lit en premier
 * (`WS_SERVER_URL` et `ATHENA_ACCESS_TOKEN`). Le secret n'y apparaît que dans
 * l'état mémoire du panneau, le temps de le copier.
 */
export function couplingCommand(serverUrl: string, secret: string): string {
  return `WS_SERVER_URL="${serverUrl}" ATHENA_ACCESS_TOKEN="${secret}" python -m client`;
}

const WILDCARD_HOSTS = new Set(["0.0.0.0", "::", "[::]", ""]);

/**
 * URL du transport réellement joignable depuis un navigateur.
 *
 * Le serveur peut écouter sur une adresse de toute interface (`0.0.0.0`, `::`) :
 * un Client ne peut pas s'y connecter, on la remplace par l'hôte par lequel la
 * Console a été ouverte. Une URL non-WebSocket est refusée.
 */
export function clientTransportUrl(
  transportUrl: string | null,
  browserHostname: string,
): string | null {
  if (transportUrl === null || transportUrl.trim() === "") {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(transportUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") {
    return null;
  }
  const host = WILDCARD_HOSTS.has(parsed.hostname)
    ? browserHostname.includes(":")
      ? `[${browserHostname.replaceAll("[", "").replaceAll("]", "")}]`
      : browserHostname
    : parsed.hostname;
  if (host === "") {
    return null;
  }
  const port = parsed.port === "" ? "" : `:${parsed.port}`;
  return `${parsed.protocol}//${host}${port}`;
}
