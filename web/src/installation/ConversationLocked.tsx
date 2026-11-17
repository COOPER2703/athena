import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { ConversationZone } from "../modules/conversation/ConversationZone";
import { ConversationIcon } from "../shared/icons";
import { Button } from "../shared/ui/Button";
import { EmptyState } from "../shared/ui/EmptyState";
import {
  GEMINI_SETTINGS_PATH,
  installationGaps,
  isConversationAvailable,
} from "./installation";

/**
 * Conversation désactivée tant qu'Athena ne peut pas parler.
 *
 * La zone reste à sa place, avec une explication franche des surfaces fermées
 * et un bouton vers le réglage manquant : jamais une promesse de panne.
 */
export function ConversationLocked() {
  const { session } = useAuth();
  const navigate = useNavigate();
  const gaps = session === undefined ? [] : installationGaps(session);
  const missingKey = !session?.geminiConfigured;

  return (
    <div className="px-5 py-8 sm:px-8 lg:px-10 lg:py-12">
      <EmptyState
        icon={<ConversationIcon className="h-5 w-5" />}
        title="Conversation désactivée"
        body={[
          "Athena ne peut pas encore parler : le transport des Clients, le Webhook et Gemini sont fermés.",
          ...gaps.map((gap) => gap.body),
          "Les Réglages et le Diagnostic restent disponibles.",
        ].join(" ")}
        hint="Un rappel reste affiché en haut de la Console tant que l'Installation n'est pas terminée."
      >
        {missingKey ? (
          <Button
            variant="primary"
            onClick={() => navigate(GEMINI_SETTINGS_PATH)}
          >
            Configurer la clé Gemini
          </Button>
        ) : null}
        <Button variant="secondary" onClick={() => navigate("/settings")}>
          Ouvrir les Réglages
        </Button>
      </EmptyState>
    </div>
  );
}

/** Route de la Conversation : le module réel, ou la zone désactivée. */
export function ConversationRoute() {
  const { session } = useAuth();
  return isConversationAvailable(session) ? (
    <ConversationZone />
  ) : (
    <ConversationLocked />
  );
}
