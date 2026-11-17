import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { fetchSession, login, logout, type Credentials } from "../api/client";
import { isUnauthorized } from "../api/errors";
import { onUnauthorized, queryKeys } from "../api/queryClient";
import { defaultZone, safeReturnPath } from "../app/routing";
import { ANONYMOUS_SESSION, type AuthSession } from "./session";

interface AuthContextValue {
  /** Identité connue ; `undefined` tant que le serveur n'a pas répondu. */
  session: AuthSession | undefined;
  isLoading: boolean;
  error: unknown;
  refetch: () => void;
  signIn: (credentials: Credentials) => Promise<void>;
  signOut: () => Promise<void>;
  signingIn: boolean;
  signingOut: boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

interface NavigationState {
  from?: unknown;
  expired?: unknown;
}

function returnPath(state: unknown): string | null {
  if (typeof state !== "object" || state === null) {
    return null;
  }
  return safeReturnPath((state as NavigationState).from);
}

/**
 * Identité de la Console : une requête mise en cache, deux mutations, et un
 * seul endroit qui traite l'expiration du Ticket.
 *
 * Aucun jeton n'est lu par JavaScript : le cookie `HttpOnly` part seul, et
 * `GET /api/session` dit seulement si le navigateur est reconnu.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const locationRef = useRef(location);
  locationRef.current = location;

  const sessionQuery = useQuery({
    queryKey: queryKeys.session,
    queryFn: () => fetchSession(),
    staleTime: 30_000,
  });

  const loginMutation = useMutation({
    mutationFn: (credentials: Credentials) => login(credentials),
  });
  const logoutMutation = useMutation({ mutationFn: () => logout() });

  const resetPrivateState = useCallback(() => {
    // Les données du Compte précédent ne doivent jamais réapparaître.
    queryClient.removeQueries({
      predicate: (query) => query.queryKey[0] !== queryKeys.session[0],
    });
  }, [queryClient]);

  const signIn = useCallback(
    async (credentials: Credentials) => {
      const session = await loginMutation.mutateAsync(credentials);
      resetPrivateState();
      queryClient.setQueryData(queryKeys.session, session);
      const from = returnPath(locationRef.current.state);
      navigate(from ?? defaultZone().path, { replace: true });
    },
    [loginMutation, navigate, queryClient, resetPrivateState],
  );

  const signOut = useCallback(async () => {
    try {
      await logoutMutation.mutateAsync();
    } catch (reason) {
      // Un Ticket déjà expiré n'empêche pas de quitter : le serveur de toute
      // façon ne reconnaît plus ce navigateur.
      if (!isUnauthorized(reason)) {
        throw reason;
      }
    }
    queryClient.clear();
    queryClient.setQueryData(queryKeys.session, ANONYMOUS_SESSION);
    navigate("/login", { replace: true });
  }, [logoutMutation, navigate, queryClient]);

  useEffect(
    () =>
      onUnauthorized(() => {
        const current = locationRef.current;
        resetPrivateState();
        queryClient.setQueryData(queryKeys.session, ANONYMOUS_SESSION);
        if (
          current.pathname === "/login" ||
          current.pathname === "/installation"
        ) {
          return;
        }
        navigate("/login", {
          replace: true,
          state: {
            from: `${current.pathname}${current.search}${current.hash}`,
            expired: true,
          },
        });
      }),
    [navigate, queryClient, resetPrivateState],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      session: sessionQuery.data,
      isLoading: sessionQuery.isPending,
      error: sessionQuery.error,
      refetch: () => {
        void sessionQuery.refetch();
      },
      signIn,
      signOut,
      signingIn: loginMutation.isPending,
      signingOut: logoutMutation.isPending,
    }),
    [
      loginMutation.isPending,
      logoutMutation.isPending,
      sessionQuery.data,
      sessionQuery.error,
      sessionQuery.isPending,
      sessionQuery.refetch,
      signIn,
      signOut,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth doit être utilisé sous AuthProvider.");
  }
  return value;
}
