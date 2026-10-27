import { useCallback, useEffect, useState } from "react";

import { fetchSession } from "./api";
import { UNAUTHORIZED_MESSAGE, screenForAuth, type AuthSession } from "./auth";
import { LoginScreen, SetupScreen } from "./AuthScreens";
import { ConsoleScreen } from "./ConsoleScreen";

/**
 * Porte d'entrée de la Console : un seul écran à la fois, choisi à partir de
 * `GET /api/session`. Pas de routeur — l'installation prime, puis un Ticket
 * Console valide ouvre la console, sinon la connexion.
 */
type Phase =
  | { phase: "loading" }
  | { phase: "setup" }
  | { phase: "login"; message?: string }
  | { phase: "console"; username: string };

function phaseForSession(session: AuthSession): Phase {
  switch (screenForAuth(session)) {
    case "setup":
      return { phase: "setup" };
    case "console":
      return { phase: "console", username: session.username ?? "" };
    default:
      return { phase: "login" };
  }
}

export function App() {
  const [phase, setPhase] = useState<Phase>({ phase: "loading" });

  const reload = useCallback(() => {
    const controller = new AbortController();
    fetchSession(controller.signal)
      .then((session) => setPhase(phaseForSession(session)))
      .catch(() => {
        if (controller.signal.aborted) {
          return;
        }
        // Serveur injoignable : l'écran de connexion reste actionnable.
        setPhase({ phase: "login" });
      });
    return () => controller.abort();
  }, []);

  useEffect(() => reload(), [reload]);

  const handleUnauthorized = useCallback(() => {
    setPhase({ phase: "login", message: UNAUTHORIZED_MESSAGE });
  }, []);

  // Déconnexion voulue : le serveur a déjà effacé le cookie, on revient au
  // login sans message d'expiration.
  const handleSignedOut = useCallback((message?: string) => {
    setPhase(
      message === undefined ? { phase: "login" } : { phase: "login", message },
    );
  }, []);

  if (phase.phase === "loading") {
    return (
      <main className="auth">
        <p className="notice" role="status">
          Chargement de la Console…
        </p>
      </main>
    );
  }

  if (phase.phase === "setup") {
    return (
      <SetupScreen
        onAuthenticated={(username) =>
          setPhase({ phase: "console", username })
        }
        onAlreadyInstalled={() => {
          reload();
        }}
      />
    );
  }

  if (phase.phase === "login") {
    return (
      <LoginScreen
        message={phase.message}
        onAuthenticated={(username) =>
          setPhase({ phase: "console", username })
        }
      />
    );
  }

  return (
    <ConsoleScreen
      username={phase.username}
      onUnauthorized={handleUnauthorized}
      onSignedOut={handleSignedOut}
    />
  );
}
