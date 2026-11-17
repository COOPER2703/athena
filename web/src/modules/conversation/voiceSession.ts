/**
 * Occupations d'Athena vues par `GET /api/status`.
 *
 * Module **pur** : il ne lit qu'un instantané brut. Une Session vocale se
 * reconnaît à son `session_id` ; un Tour texte ne crée aucune Session, même
 * quand l'état du noyau est `LiveActive` (ADR-0023). La Conversation s'en sert
 * pour prévenir qu'un message risque d'être refusé, jamais pour bloquer
 * l'envoi : le refus `busy` reste la seule autorité.
 */

export function voiceSessionActive(status: unknown): boolean {
  if (typeof status !== "object" || status === null) {
    return false;
  }
  const session = (status as { session?: unknown }).session;
  if (typeof session !== "object" || session === null) {
    return false;
  }
  const sessionId = (session as { session_id?: unknown }).session_id;
  return typeof sessionId === "string" && sessionId !== "";
}
