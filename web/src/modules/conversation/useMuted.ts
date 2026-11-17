import { useCallback, useState } from "react";

import { readMuted, writeMuted } from "../../shared/preferences";

/** Stockage du poste, ou `undefined` hors navigateur (rendu de test). */
function localStore(): Storage | undefined {
  if (typeof window === "undefined") {
    return undefined;
  }
  return window.localStorage;
}

/**
 * Sourdine de la Conversation : active par défaut, mémorisée sur le poste.
 *
 * Ce n'est pas un Secret : seulement un choix d'interface. Un stockage refusé
 * laisse la sourdine active pour la visite en cours.
 */
export function useMuted(): {
  muted: boolean;
  setMuted: (next: boolean) => void;
  toggle: () => void;
} {
  const [muted, setMutedState] = useState<boolean>(() => readMuted(localStore()));

  const setMuted = useCallback((next: boolean) => {
    setMutedState(next);
    writeMuted(localStore(), next);
  }, []);

  const toggle = useCallback(() => {
    setMutedState((current) => {
      const next = !current;
      writeMuted(localStore(), next);
      return next;
    });
  }, []);

  return { muted, setMuted, toggle };
}
