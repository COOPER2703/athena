import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { RefreshIcon } from "../shared/icons";
import { Button } from "../shared/ui/Button";
import { GEMINI_SETTINGS_PATH, installationGaps } from "./installation";

/**
 * Rappel permanent de l'Installation incomplète.
 *
 * Il n'est **jamais masquable** : tant qu'une clé Gemini utilisable manque (ou
 * que les Secrets sont illisibles, ou qu'un redémarrage est requis), il occupe
 * le haut de toutes les zones et mène au réglage concerné.
 */
export function InstallBanner() {
  const { session, refetch } = useAuth();
  const navigate = useNavigate();

  if (session === undefined || !session.authenticated) {
    return null;
  }

  const gaps = installationGaps(session);
  const [first, ...rest] = gaps;
  if (first === undefined) {
    return null;
  }

  return (
    <section
      aria-label="Installation incomplète"
      role="status"
      className="border-b border-warning-line bg-warning-soft"
    >
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 py-3 sm:px-8 lg:px-10">
        <div className="min-w-0">
          <p className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-warning">
            Installation incomplète · rappel permanent
          </p>
          <p className="mt-1 max-w-prose text-sm leading-relaxed text-warning">
            <span className="font-medium">{first.title} — </span>
            {first.body}
          </p>
          {rest.length > 0 ? (
            <p className="mt-1 text-xs leading-relaxed text-warning">
              Aussi : {rest.map((gap) => gap.title).join(" · ")}
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {session.geminiConfigured ? (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => navigate("/settings")}
            >
              Ouvrir les Réglages
            </Button>
          ) : (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => navigate(GEMINI_SETTINGS_PATH)}
            >
              Configurer la clé Gemini
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            icon={<RefreshIcon />}
            onClick={refetch}
          >
            Actualiser
          </Button>
        </div>
      </div>
    </section>
  );
}
