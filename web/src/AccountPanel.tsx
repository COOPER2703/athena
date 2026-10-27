import { useState, type FormEvent } from "react";

import {
  LOGOUT_ALL_MESSAGE,
  MIN_PASSWORD_LENGTH,
  passwordChangeErrorMessage,
  passwordChangePayload,
  validatePasswordChange,
  type PasswordChangeDraft,
} from "./account";
import { changePassword, logout, logoutAll } from "./api";
import { isUnauthorized } from "./auth";
import "./account.css";

const EMPTY_DRAFT: PasswordChangeDraft = {
  current: "",
  next: "",
  confirmation: "",
};

type Pending = "password" | "logout" | "logout-all" | null;

interface AccountPanelProps {
  /** Nom du Compte connecté, rappelé dans le panneau. */
  username: string;
  /** Appelé si le Ticket Console a expiré ou été révoqué pendant l'action. */
  onUnauthorized: () => void;
  /** Appelé après une déconnexion : `App` revient à l'écran de connexion. */
  onSignedOut: (message?: string) => void;
}

/**
 * Panneau Compte : changement de mot de passe, déconnexion, déconnexion totale.
 *
 * Le changement exige le mot de passe actuel et laisse le Ticket courant
 * valide ; les autres navigateurs sont révoqués côté serveur. Les deux
 * déconnexions effacent le cookie et ramènent à l'écran de connexion.
 */
export function AccountPanel({
  username,
  onUnauthorized,
  onSignedOut,
}: AccountPanelProps) {
  const [draft, setDraft] = useState<PasswordChangeDraft>(EMPTY_DRAFT);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending>(null);

  function update(field: keyof PasswordChangeDraft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setError(null);
    setNotice(null);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending !== null) {
      return;
    }
    const problem = validatePasswordChange(draft);
    if (problem !== null) {
      setNotice(null);
      setError(problem);
      return;
    }
    setPending("password");
    setError(null);
    setNotice(null);
    try {
      await changePassword(passwordChangePayload(draft));
      setDraft(EMPTY_DRAFT);
      setNotice(
        "Mot de passe changé. Les autres Tickets Console du Compte ont été révoqués.",
      );
    } catch (reason: unknown) {
      if (isUnauthorized(reason)) {
        onUnauthorized();
        return;
      }
      setError(passwordChangeErrorMessage(reason));
    } finally {
      setPending(null);
    }
  }

  async function signOut(all: boolean) {
    if (pending !== null) {
      return;
    }
    setPending(all ? "logout-all" : "logout");
    setError(null);
    setNotice(null);
    try {
      if (all) {
        await logoutAll();
      } else {
        await logout();
      }
      onSignedOut(all ? LOGOUT_ALL_MESSAGE : undefined);
    } catch (reason: unknown) {
      if (isUnauthorized(reason)) {
        // Le Ticket était déjà révoqué : le résultat voulu est atteint.
        onSignedOut();
        return;
      }
      setError(passwordChangeErrorMessage(reason));
    } finally {
      setPending(null);
    }
  }

  return (
    <section className="panel account" aria-labelledby="account-title">
      <div className="panel__header">
        <h2 className="panel__title" id="account-title">
          Compte
        </h2>
        <span className="mono">{username}</span>
      </div>

      <form className="account__form" onSubmit={handleSubmit}>
        <p className="account__subtitle">
          Changer le mot de passe révoque les autres Tickets Console du Compte ;
          celui de ce navigateur reste valide.
        </p>

        <div className="account__fields">
          <label className="field" htmlFor="account-current">
            <span className="field__label">Mot de passe actuel</span>
            <input
              className="field__input"
              id="account-current"
              type="password"
              value={draft.current}
              onChange={(event) => update("current", event.target.value)}
              autoComplete="current-password"
              disabled={pending !== null}
            />
          </label>

          <label className="field" htmlFor="account-next">
            <span className="field__label">Nouveau mot de passe</span>
            <input
              className="field__input"
              id="account-next"
              type="password"
              value={draft.next}
              onChange={(event) => update("next", event.target.value)}
              autoComplete="new-password"
              disabled={pending !== null}
            />
            <span className="account__hint">
              Au moins {MIN_PASSWORD_LENGTH} caractères.
            </span>
          </label>

          <label className="field" htmlFor="account-confirmation">
            <span className="field__label">Confirmer le mot de passe</span>
            <input
              className="field__input"
              id="account-confirmation"
              type="password"
              value={draft.confirmation}
              onChange={(event) => update("confirmation", event.target.value)}
              autoComplete="new-password"
              disabled={pending !== null}
            />
          </label>
        </div>

        <div className="account__actions">
          <button className="button" type="submit" disabled={pending !== null}>
            {pending === "password" ? "Changement…" : "Changer le mot de passe"}
          </button>
          <span className="account__spacer" />
          <button
            className="button"
            type="button"
            onClick={() => void signOut(false)}
            disabled={pending !== null}
          >
            {pending === "logout" ? "Déconnexion…" : "Se déconnecter"}
          </button>
          <button
            className="button button--danger"
            type="button"
            onClick={() => void signOut(true)}
            disabled={pending !== null}
          >
            {pending === "logout-all"
              ? "Déconnexion…"
              : "Déconnecter partout"}
          </button>
        </div>

        {notice !== null ? (
          <p className="notice notice--success" role="status">
            {notice}
          </p>
        ) : null}
        {error !== null ? (
          <p className="notice notice--error" role="alert">
            {error}
          </p>
        ) : null}
      </form>
    </section>
  );
}
