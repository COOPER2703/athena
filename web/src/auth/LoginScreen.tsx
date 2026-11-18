import { useState, type FormEvent } from "react";
import { useLocation } from "react-router-dom";

import {
  describeError,
  describeFieldErrors,
  isApiError,
} from "../api/errors";
import { useDraft } from "../shared/useDraft";
import { Button } from "../shared/ui/Button";
import { Notice } from "../shared/ui/Notice";
import { TextField } from "../shared/ui/TextField";
import { useAuth } from "./AuthProvider";
import { AuthLayout } from "./AuthLayout";

interface LoginState {
  expired?: unknown;
}

/**
 * Connexion : nom du Compte et mot de passe, jamais de jeton lisible par
 * JavaScript. Le nom saisi survit à l'expiration du Ticket ; le mot de passe,
 * lui, reste en mémoire seulement.
 */
export function LoginScreen() {
  const { signIn, signingIn } = useAuth();
  const location = useLocation();
  const expired =
    typeof location.state === "object" &&
    location.state !== null &&
    (location.state as LoginState).expired === true;

  const [username, setUsername] = useDraft("login.username");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const nextFields: Record<string, string> = {};
    if (username.trim() === "") {
      nextFields.username = "Saisissez le nom du Compte.";
    }
    if (password === "") {
      nextFields.password = "Saisissez le mot de passe.";
    }
    setFields(nextFields);
    if (Object.keys(nextFields).length > 0) {
      return;
    }

    try {
      await signIn({ username: username.trim(), password });
    } catch (reason) {
      setError(describeError(reason));
      setFields(describeFieldErrors(reason));
      if (isApiError(reason) && reason.status === 401) {
        setPassword("");
      }
    }
  }

  return (
    <AuthLayout
      eyebrow="Identité"
      title="Connexion"
      summary="Entrez le nom du Compte et son mot de passe pour ouvrir cette instance."
      footer="Le Ticket Console reste dans ce navigateur et se renouvelle tant qu'il est utilisé."
    >
      {expired ? (
        <Notice tone="warning" title="Ticket Console expiré" className="mb-5">
          Reconnectez-vous pour reprendre où vous en étiez. La saisie conservée
          dans cet onglet sera restaurée.
        </Notice>
      ) : null}

      {error ? (
        <Notice tone="danger" className="mb-5">
          {error}
        </Notice>
      ) : null}

      <form onSubmit={submit} noValidate aria-busy={signingIn}>
        <div className="flex flex-col gap-4">
          <TextField
            label="Nom du Compte"
            name="username"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            error={fields.username}
            disabled={signingIn}
          />
          <TextField
            label="Mot de passe"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            error={fields.password}
            disabled={signingIn}
          />
        </div>
        <Button
          type="submit"
          variant="primary"
          className="mt-6 w-full"
          disabled={signingIn}
        >
          {signingIn ? "Connexion…" : "Se connecter"}
        </Button>
      </form>
    </AuthLayout>
  );
}
