import { useEffect, useRef, useState } from "react";

export type RecoveryPhase = "idle" | "waiting" | "ready" | "timeout";

const FIRST_DELAY_MS = 600;
const MAX_DELAY_MS = 3_000;
const TIMEOUT_MS = 90_000;

function delayFor(attempt: number): number {
  return Math.min(FIRST_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
}

/**
 * Attente du retour du serveur après un redémarrage automatique.
 *
 * La connexion tombe juste après le `PUT` : sonder `/healthz` évite de traiter
 * cette coupure comme une erreur. Tant que la Console reste joignable, la page
 * se resynchronise sans rechargement ; au-delà du délai, l'écran explique la
 * situation plutôt que de tourner indéfiniment.
 */
export function useRestartRecovery(
  enabled: boolean,
  onRecovered: () => void,
): RecoveryPhase {
  const [phase, setPhase] = useState<RecoveryPhase>("idle");
  const callback = useRef(onRecovered);
  callback.current = onRecovered;

  useEffect(() => {
    if (!enabled) {
      setPhase("idle");
      return;
    }

    let cancelled = false;
    let timer: number | undefined;
    let attempt = 0;
    const startedAt = Date.now();
    setPhase("waiting");

    const poll = async (): Promise<void> => {
      if (cancelled) {
        return;
      }
      let healthy = false;
      try {
        // `/api/session` est public et proxifié en développement, contrairement
        // à `/healthz` : c'est la même sonde que la coquille utilise au boot.
        const response = await fetch("/api/session", {
          credentials: "same-origin",
          cache: "no-store",
        });
        healthy = response.ok;
      } catch {
        healthy = false;
      }
      if (cancelled) {
        return;
      }
      if (healthy) {
        setPhase("ready");
        callback.current();
        return;
      }
      if (Date.now() - startedAt >= TIMEOUT_MS) {
        setPhase("timeout");
        return;
      }
      const delay = delayFor(attempt);
      attempt += 1;
      timer = window.setTimeout(() => {
        void poll();
      }, delay);
    };

    timer = window.setTimeout(() => {
      void poll();
    }, FIRST_DELAY_MS);

    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [enabled]);

  return phase;
}
