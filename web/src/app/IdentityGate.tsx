import { useEffect, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { ServerUnavailable } from "./screens/ServerUnavailable";
import { SplashScreen } from "./screens/SplashScreen";
import { defaultZone, safeReturnPath, screenFor } from "./routing";

function returnPath(state: unknown): string | null {
  if (typeof state !== "object" || state === null) {
    return null;
  }
  return safeReturnPath((state as { from?: unknown }).from);
}

/**
 * Aiguillage d'identité : installation, connexion ou application.
 *
 * C'est le seul endroit qui décide de l'écran racine. Une route protégée
 * demandée sans Ticket valide est mémorisée pour y revenir après connexion.
 */
export function IdentityGate({ children }: { children: ReactNode }) {
  const { session, isLoading, error, refetch } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const screen = screenFor(session);

  useEffect(() => {
    if (session === undefined) {
      return;
    }
    const path = location.pathname;

    if (screen === "installation") {
      if (path !== "/installation") {
        navigate("/installation", { replace: true });
      }
      return;
    }

    if (screen === "login") {
      if (path !== "/login") {
        navigate("/login", {
          replace: true,
          state:
            path === "/installation"
              ? undefined
              : { from: `${path}${location.search}` },
        });
      }
      return;
    }

    if (path === "/login" || path === "/installation") {
      navigate(returnPath(location.state) ?? defaultZone().path, {
        replace: true,
      });
    }
  }, [
    location.pathname,
    location.search,
    location.state,
    navigate,
    screen,
    session,
  ]);

  if (session === undefined) {
    if (!isLoading && error !== null && error !== undefined) {
      return <ServerUnavailable error={error} onRetry={refetch} />;
    }
    return <SplashScreen />;
  }

  return <>{children}</>;
}
