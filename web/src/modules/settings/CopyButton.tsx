import { useEffect, useState } from "react";

import { Button, type ButtonSize } from "../../shared/ui/Button";
import { copyText } from "./clipboard";

const COPIED_MS = 2_000;

export interface CopyButtonProps {
  text: string;
  label?: string;
  copiedLabel?: string;
  size?: ButtonSize;
  disabled?: boolean;
}

/**
 * Bouton de copie avec retour visible.
 *
 * Le texte copié — un secret, une commande — n'est jamais conservé ailleurs
 * que dans l'état d'affichage : ce composant ne le stocke pas et ne le trace
 * pas. Un échec propose la sélection manuelle.
 */
export function CopyButton({
  text,
  label = "Copier",
  copiedLabel = "Copié",
  size = "sm",
  disabled = false,
}: CopyButtonProps) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    if (state !== "copied") {
      return;
    }
    const timer = window.setTimeout(() => {
      setState("idle");
    }, COPIED_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [state]);

  return (
    <span className="inline-flex flex-col gap-1">
      <Button
        type="button"
        variant="secondary"
        size={size}
        disabled={disabled}
        onClick={() => {
          void copyText(text).then((copied) => {
            setState(copied ? "copied" : "failed");
          });
        }}
      >
        {state === "copied" ? copiedLabel : label}
      </Button>
      {state === "failed" ? (
        <span role="alert" className="text-xs leading-relaxed text-danger">
          Copie impossible : sélectionnez le texte puis copiez-le.
        </span>
      ) : null}
    </span>
  );
}
