import { useState, type FormEvent } from "react";

import { MIN_PASSWORD_LENGTH } from "./account";
import { authErrorMessage, errorStatus } from "./auth";
import { login, setupAccount } from "./api";
import "./auth.css";

interface SetupScreenProps {
  /** Appelé avec le nom du Compte après création et ouverture du Ticket. */
  onAuthenticated: (username: string) => void;
  /** L'instance s'est installée entre-temps : on bascule vers la connexion. */
  onAlreadyInstalled: () => void;
}

/**
 * Écran d'installation : Code d'installation, nom d'utilisateur, mot de passe.
 *
 * Il n'est affiché que tant qu'aucun Compte n'existe (`GET /api/session`). Le
 * code est à usage unique et se lit dans le journal du serveur.
 */
export function SetupScreen({
  onAuthenticated,
  onAlreadyInstalled,
}: SetupScreenProps) {
  const [code, setCode] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) {
      return;
    }
    if (password !== confirmation) {
      setError("Les deux mots de passe ne correspondent pas.");
      return;
    }
    setError(null);
    setPending(true);
    try {
      const session = await setupAccount({ code, username, password });
      onAuthenticated(session.username ?? username);
    } catch (reason: unknown) {
      if (errorStatus(reason) === 409) {
        onAlreadyInstalled();
        return;
      }
      setError(authErrorMessage(reason));
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="auth">
      <section className="panel auth__card" aria-labelledby="setup-title">
        <div className="panel__header">
          <h1 className="panel__title" id="setup-title">
            Installer Athena
          </h1>
        </div>
        <p className="auth__subtitle">
          Aucun Compte n'existe encore. Saisissez le Code d'installation écrit
          dans le journal du serveur (<code>logs/server.log</code>) pour créer
          l'unique Compte admin.
        </p>

        <form className="auth__form" onSubmit={handleSubmit}>
          <label className="field" htmlFor="setup-code">
            <span className="field__label">Code d'installation</span>
            <input
              className="field__input mono"
              id="setup-code"
              type="text"
              value={code}
              onChange={(event) => {
                setCode(event.target.value);
                setError(null);
              }}
              autoComplete="off"
              spellCheck={false}
              autoFocus
              disabled={pending}
            />
          </label>

          <label className="field" htmlFor="setup-username">
            <span className="field__label">Nom d'utilisateur</span>
            <input
              className="field__input"
              id="setup-username"
              type="text"
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
                setError(null);
              }}
              autoComplete="username"
              spellCheck={false}
              disabled={pending}
            />
          </label>

          <label className="field" htmlFor="setup-password">
            <span className="field__label">Mot de passe</span>
            <input
              className="field__input"
              id="setup-password"
              type="password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
                setError(null);
              }}
              autoComplete="new-password"
              disabled={pending}
            />
            <span className="auth__hint">
              Au moins {MIN_PASSWORD_LENGTH} caractères.
            </span>
          </label>

          <label className="field" htmlFor="setup-confirmation">
            <span className="field__label">Confirmer le mot de passe</span>
            <input
              className="field__input"
              id="setup-confirmation"
              type="password"
              value={confirmation}
              onChange={(event) => {
                setConfirmation(event.target.value);
                setError(null);
              }}
              autoComplete="new-password"
              disabled={pending}
            />
          </label>

          <div className="auth__actions">
            <button className="button" type="submit" disabled={pending}>
              {pending ? "Création…" : "Créer le Compte"}
            </button>
          </div>

          {error !== null ? (
            <p className="notice notice--error" role="alert">
              {error}
            </p>
          ) : null}
        </form>
      </section>
    </main>
  );
}

interface LoginScreenProps {
  /** Message porté par le retour au login (Ticket expiré, installation finie). */
  message?: string;
  onAuthenticated: (username: string) => void;
}

/** Écran de connexion : nom d'utilisateur et mot de passe du Compte. */
export function LoginScreen({ message, onAuthenticated }: LoginScreenProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) {
      return;
    }
    setError(null);
    setPending(true);
    try {
      const session = await login({ username, password });
      onAuthenticated(session.username ?? username);
    } catch (reason: unknown) {
      setError(authErrorMessage(reason));
    } finally {
      setPending(false);
    }
  }

  const notice = error ?? message ?? null;

  return (
    <main className="auth">
      <section className="panel auth__card" aria-labelledby="login-title">
        <div className="panel__header">
          <h1 className="panel__title" id="login-title">
            Connexion
          </h1>
        </div>
        <p className="auth__subtitle">
          Athena est installé. Connectez-vous avec le Compte admin.
        </p>

        <form className="auth__form" onSubmit={handleSubmit}>
          <label className="field" htmlFor="login-username">
            <span className="field__label">Nom d'utilisateur</span>
            <input
              className="field__input"
              id="login-username"
              type="text"
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
                setError(null);
              }}
              autoComplete="username"
              spellCheck={false}
              autoFocus
              disabled={pending}
            />
          </label>

          <label className="field" htmlFor="login-password">
            <span className="field__label">Mot de passe</span>
            <input
              className="field__input"
              id="login-password"
              type="password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
                setError(null);
              }}
              autoComplete="current-password"
              disabled={pending}
            />
          </label>

          <div className="auth__actions">
            <button className="button" type="submit" disabled={pending}>
              {pending ? "Connexion…" : "Se connecter"}
            </button>
          </div>

          {notice !== null ? (
            <p className="notice notice--error" role="alert">
              {notice}
            </p>
          ) : null}
        </form>
      </section>
    </main>
  );
}
