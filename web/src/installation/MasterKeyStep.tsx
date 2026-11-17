import { useId, useState } from "react";

import { Button } from "../shared/ui/Button";
import { Notice } from "../shared/ui/Notice";

export interface MasterKeyStepProps {
  /** Clé maître rendue par `POST /api/setup`, `null` si un déploiement en avait déjà une. */
  masterKey: string | null;
  onContinue: () => void;
}

/**
 * Affichage **unique** de la clé maître.
 *
 * La clé ne vit que dans l'état mémoire de cet écran : ni stockage, ni URL, ni
 * cache de requête. Quitter l'étape l'efface, et le serveur ne la rendra plus.
 */
export function MasterKeyStep({ masterKey, onContinue }: MasterKeyStepProps) {
  const [noted, setNoted] = useState(false);
  const [copy, setCopy] = useState<"idle" | "done" | "failed">("idle");
  const confirmationId = useId();

  async function copyKey() {
    if (masterKey === null) {
      return;
    }
    try {
      await navigator.clipboard.writeText(masterKey);
      setCopy("done");
    } catch {
      // Contexte non sécurisé ou permission refusée : la sélection manuelle
      // reste possible, et l'écran le dit.
      setCopy("failed");
    }
  }

  if (masterKey === null) {
    return (
      <>
        <Notice tone="info" className="mb-5">
          Aucune clé maître n'a été créée : un déploiement en avait déjà fourni
          une. Elle n'est pas réaffichée, et il n'y a rien à noter ici.
        </Notice>
        <Button variant="primary" className="w-full" onClick={onContinue}>
          Continuer
        </Button>
      </>
    );
  }

  return (
    <>
      <div role="alert" className="mb-5 rounded-sm border border-warning-line bg-warning-soft px-4 py-3">
        <p className="font-medium text-warning">
          Notez cette clé maintenant
        </p>
        <ul className="mt-2 flex list-disc flex-col gap-1 pl-5 text-sm leading-relaxed text-warning">
          <li>Elle ne sera plus jamais affichée, ni ici, ni ailleurs.</li>
          <li>
            Le magasin de réglages et cette clé forment une paire : sauvegarder
            l'un sans l'autre rend les Secrets illisibles.
          </li>
          <li>
            Sans elle, il faudra ressaisir la clé Gemini après une restauration.
          </li>
        </ul>
      </div>

      <div className="rounded-sm border border-border bg-canvas p-3">
        <p className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted">
          Clé maître
        </p>
        <pre
          aria-label="Clé maître"
          className="mt-2 select-all break-all whitespace-pre-wrap font-mono text-sm leading-relaxed text-text"
        >
          {masterKey}
        </pre>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="secondary" onClick={() => void copyKey()}>
          Copier la clé
        </Button>
        {copy === "done" ? (
          <span role="status" className="text-xs text-accent">
            Clé copiée dans le presse-papiers.
          </span>
        ) : null}
        {copy === "failed" ? (
          <span role="status" className="text-xs text-warning">
            Copie impossible ici : sélectionnez la clé puis copiez-la.
          </span>
        ) : null}
      </div>

      <div className="mt-6 flex items-start gap-3 border-t border-border pt-4">
        <input
          id={confirmationId}
          type="checkbox"
          autoFocus
          checked={noted}
          onChange={(event) => setNoted(event.target.checked)}
          className="mt-1 h-4 w-4 shrink-0 accent-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
        />
        <label htmlFor={confirmationId} className="text-sm leading-relaxed text-muted">
          J'ai noté la clé maître dans un endroit sûr, séparé du magasin de
          réglages.
        </label>
      </div>

      <Button
        variant="primary"
        className="mt-6 w-full"
        onClick={onContinue}
        disabled={!noted}
      >
        Continuer vers la clé Gemini
      </Button>
    </>
  );
}
