/**
 * Logique d'affichage du panneau Session, sans navigateur.
 *
 * Ces fonctions sont pures : elles traduisent l'instantané de `/api/status` en
 * libellés français. Le contrat JSON reste en `snake_case` et en anglais côté
 * serveur ; seule la présentation est traduite ici.
 */

/** Client branché, tel que le serveur le décrit dans l'instantané. */
export interface ClientInfo {
  client_id: string;
  client_name: string;
  platform: string;
}

/** Session du noyau, telle que l'instantané la décrit. */
export interface SessionStatus {
  state: string;
  session_id: string;
  connection_id: string;
  active_client_id: string;
}

/** Résumé prêt à afficher dans le panneau Session. */
export interface SessionSummary {
  stateLabel: string;
  sessionId: string;
  activeClient: string;
}

/** États connus du noyau, traduits pour la Console. */
const SESSION_STATE_LABELS: Readonly<Record<string, string>> = {
  Idle: "Inactive",
  LiveConnecting: "Connexion Live",
  LiveActive: "Live actif",
  SessionVisible: "Session en cours",
  Closing: "Fermeture",
};

/** Libellé français d'un état de Session ; l'état brut passe s'il est inconnu. */
export function formatSessionState(state: string): string {
  if (!state) {
    return "Inconnu";
  }
  return SESSION_STATE_LABELS[state] ?? state;
}

/** Identifiant raccourci pour rester lisible quand le nom du Client manque. */
export function shortClientId(clientId: string, maxLength = 8): string {
  if (clientId.length <= maxLength) {
    return clientId;
  }
  return `${clientId.slice(0, maxLength)}…`;
}

/** Nom du Client actif, son identifiant court à défaut, « Aucun » sinon. */
export function activeClientLabel(
  activeClientId: string,
  clients: readonly ClientInfo[],
): string {
  if (!activeClientId) {
    return "Aucun";
  }
  const client = clients.find((item) => item.client_id === activeClientId);
  return client?.client_name ? client.client_name : shortClientId(activeClientId);
}

/** Résumé de la Session affiché en tête du panneau. */
export function sessionSummary(
  session: SessionStatus,
  clients: readonly ClientInfo[],
): SessionSummary {
  return {
    stateLabel: formatSessionState(session.state),
    sessionId: session.session_id || "—",
    activeClient: activeClientLabel(session.active_client_id, clients),
  };
}
