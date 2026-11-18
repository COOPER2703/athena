/**
 * Secrets de la Console : écriture seule.
 *
 * Aucune valeur n'est jamais relue : `GET /api/secrets` ne rend que des noms,
 * un état et une date. Ce module n'expose donc que la lecture des états,
 * l'écriture et l'effacement ; le corps d'une écriture n'est jamais conservé.
 */

import { jsonBody, requestJson, type Fetcher } from "./client";

/** Nom du Secret Gemini : c'est aussi le nom de la variable attendue. */
export const GEMINI_SECRET = "GEMINI_API_KEY";

/** État public d'un Secret : ni valeur, ni empreinte. */
export interface SecretStatus {
  name: string;
  configured: boolean;
  /** Date de mise à jour, en secondes epoch, ou `null`. */
  updatedAt: number | null;
}

/** Réponse normalisée de `GET /api/secrets`. */
export interface SecretsPayload {
  secrets: readonly SecretStatus[];
  /** Vrai quand la clé maître ne déchiffre plus le magasin. */
  unreadable: boolean;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

/** Normalise l'état d'un Secret ; `null` si son nom manque. */
export function normalizeSecretStatus(raw: unknown): SecretStatus | null {
  const record = asRecord(raw);
  const name = typeof record.name === "string" ? record.name.trim() : "";
  if (name === "") {
    return null;
  }
  const updatedAt = record.updated_at;
  return {
    name,
    configured: record.configured === true,
    updatedAt:
      typeof updatedAt === "number" && Number.isFinite(updatedAt)
        ? updatedAt
        : null,
  };
}

/** Lit une liste de Secrets, en ignorant toute entrée inutilisable. */
export function normalizeSecrets(body: unknown): SecretsPayload {
  const record = asRecord(body);
  const rawSecrets = Array.isArray(record.secrets) ? record.secrets : [];
  const secrets: SecretStatus[] = [];
  for (const raw of rawSecrets) {
    const secret = normalizeSecretStatus(raw);
    if (secret !== null) {
      secrets.push(secret);
    }
  }
  return { secrets, unreadable: record.secrets_unreadable === true };
}

/** Lit les noms et états des Secrets. */
export async function fetchSecrets(
  fetcher?: Fetcher,
): Promise<SecretsPayload> {
  return normalizeSecrets(await requestJson("/api/secrets", {}, fetcher));
}

/** Enregistre un Secret ; le serveur le valide avant de le chiffrer. */
export async function saveSecret(
  name: string,
  value: string,
  fetcher?: Fetcher,
): Promise<void> {
  await requestJson(
    `/api/secrets/${encodeURIComponent(name)}`,
    { method: "PUT", ...jsonBody({ value }) },
    fetcher,
  );
}

/** Efface un Secret par son nom ; une valeur effacée ne revient jamais. */
export async function deleteSecret(
  name: string,
  fetcher?: Fetcher,
): Promise<void> {
  await requestJson(
    `/api/secrets/${encodeURIComponent(name)}`,
    { method: "DELETE" },
    fetcher,
  );
}
