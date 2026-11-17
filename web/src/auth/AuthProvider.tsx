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

import {
  fetchSession,
  login,
  logout,
  logoutAll,
  type Credentials,
} from "../api/client";
import { isUnauthorized } from "../api/errors";
import { onUnauthorized, queryKeys } from "../api/queryClient";
import { setupAccount } from "../api/setup";
import { defaultZone, safeReturnPath } from "../app/routing";
import { ANONYMOUS_SESSION, type AuthSession } from "./session";

interface AuthContextValue {
  /** Identité connue ; `undefined` tant que le serveur n'a pas répondu. */
  session: AuthSession | undefined;
  isLoading: boolean;
  error: unknown;
  refetch: () => void;
  signIn: (credentials: Credentials) => Promise<void>;
  /** Crée l'unique Compte ; rend la clé maître, à afficher une seule fois. */
  installAccount: (credentials: Credentials) => Promise<string | null>;
  signOut: () => Promise<void>;
  /** « Déconnecter partout » : révoque tous les Tickets du Compte. */
  signOutEverywhere: () => Promise<void>;
  signingIn: boolean;
  installing: boolean;
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
  const setupMutation = useMutation({
    mutationFn: (credentials: Credentials) => setupAccount(credentials),
  });
  const logoutMutation = useMutation({ mutationFn: () => logout() });
  const logoutAllMutation = useMutation({ mutationFn: () => logoutAll() });

  const resetPrivateState = useCallback(() => {
    // Les données du Compte précédent ne doivent jamais réapparaître.
    queryClient.removeQueries({
      predicate: (query) => query.queryKey[0] !== queryKeys.session[0],
    });
  }, [queryClient]);

  const leave = useCallback(() => {
    queryClient.clear();
    queryClient.setQueryData(queryKeys.session, ANONYMOUS_SESSION);
    navigate("/login", { replace: true });
  }, [navigate, queryClient]);

  const signIn = useCallback(
    async (credentials: Credentials) => {
      const session = await loginMutation.mutateAsync(credentials);
      resetPrivateState();
      queryClient.setQueryData(queryKeys.session, session);
      // La réponse de connexion ne décrit pas l'Installation : on relit l'état
      // avant d'afficher la coquille, pour que le rappel de la clé Gemini soit
      // juste dès la première image.
      await sessionQuery.refetch();
      const from = returnPath(locationRef.current.state);
      navigate(from ?? defaultZone().path, { replace: true });
    },
    [
      loginMutation,
      navigate,
      queryClient,
      resetPrivateState,
      sessionQuery.refetch,
    ],
  );

  const installAccount = useCallback(
    async (credentials: Credentials) => {
      const { session, masterKey } = await setupMutation.mutateAsync(credentials);
      resetPrivateState();
      queryClient.setQueryData(queryKeys.session, session);
      // La clé Gemini n'est pas dans la réponse d'Installation : la relecture
      // donne l'état exact, mais l'écran guidé reste maître de sa progression.
      await sessionQuery.refetch();
      return masterKey;
    },
    [queryClient, resetPrivateState, sessionQuery.refetch, setupMutation],
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
    leave();
  }, [leave, logoutMutation]);

  const signOutEverywhere = useCallback(async () => {
    await logoutAllMutation.mutateAsync();
    leave();
  }, [leave, logoutAllMutation]);

  useEffect(
    () =>
      onUnauthorized(() => {
        const current = locationRef.current;
        resetPrivateState();
        queryClient.setQueryData(queryKeys.session, ANONYMOUS_SESSION);
        if (current.pathname === "/login") {
          return;
        }
        // Un Ticket peut expirer au milieu du parcours d'Installation : on
        // revient à la connexion avec la route demandée, pour reprendre à
        // l'étape en cours (la saisie secrète, elle, n'est jamais conservée).
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
      installAccount,
      signOut,
      signOutEverywhere,
      signingIn: loginMutation.isPending,
      installing: setupMutation.isPending,
      signingOut: logoutMutation.isPending || logoutAllMutation.isPending,
    }),
    [
      installAccount,
      loginMutation.isPending,
      logoutAllMutation.isPending,
      logoutMutation.isPending,
      sessionQuery.data,
      sessionQuery.error,
      sessionQuery.isPending,
      sessionQuery.refetch,
      setupMutation.isPending,
      signIn,
      signOut,
      signOutEverywhere,
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
