import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { RefreshIcon } from "../shared/icons";
import { Button } from "../shared/ui/Button";
import { Notice } from "../shared/ui/Notice";
import { GEMINI_SETTINGS_PATH, installationGaps } from "./installation";

export interface SummaryStepProps {
  /** Termine le parcours et ouvre l'application. */
  onFinish: () => void;
}

/**
 * Récapitulatif : ce qui reste à faire, et rien d'autre.
 *
 * L'Installation se termine dès qu'une clé Gemini utilisable existe : aucune
 * autre action n'est demandée. Tant qu'il manque quelque chose, le récapitulatif
 * le dit et mène au réglage concerné.
 */
export function SummaryStep({ onFinish }: SummaryStepProps) {
  const { session, refetch } = useAuth();
  const navigate = useNavigate();

  if (session === undefined) {
    return null;
  }

  const gaps = installationGaps(session);

  if (gaps.length === 0) {
    return (
      <>
        <Notice tone="success" title="Installation terminée" className="mb-5">
          Le Compte existe et une clé Gemini utilisable est enregistrée :
          Athena peut parler et la Conversation est ouverte.
        </Notice>
        <Button
          variant="primary"
          className="w-full"
          autoFocus
          onClick={onFinish}
        >
          Ouvrir la Conversation
        </Button>
      </>
    );
  }

  return (
    <>
      <Notice tone="warning" title="Il reste à faire" className="mb-5">
        <span>
          L'Installation n'est pas encore complète. Athena reste utilisable pour
          les Réglages et le Diagnostic, mais pas pour la Conversation.
        </span>
      </Notice>

      <ul className="flex flex-col gap-3">
        {gaps.map((gap) => (
          <li
            key={gap.id}
            className="rounded-sm border border-border bg-canvas px-4 py-3"
          >
            <p className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-warning">
              {gap.title}
            </p>
            <p className="mt-1 text-sm leading-relaxed text-muted">{gap.body}</p>
          </li>
        ))}
      </ul>

      <div className="mt-6 flex flex-col gap-2">
        <Button
          variant="primary"
          className="w-full"
          autoFocus
          onClick={() =>
            navigate(
              session.geminiConfigured ? "/settings" : GEMINI_SETTINGS_PATH,
            )
          }
        >
          {session.geminiConfigured
            ? "Ouvrir les Réglages"
            : "Configurer la clé Gemini"}
        </Button>
        <Button
          variant="secondary"
          className="w-full"
          icon={<RefreshIcon />}
          onClick={refetch}
        >
          Actualiser l'état
        </Button>
        <Button variant="ghost" className="w-full" onClick={onFinish}>
          Continuer vers la Console
        </Button>
      </div>

      <p className="mt-4 border-t border-border pt-4 text-xs leading-relaxed text-subtle">
        Dès qu'une clé utilisable est enregistrée, l'Installation se termine
        sans autre action : le rappel disparaît et la Conversation s'ouvre.
      </p>
    </>
  );
}
