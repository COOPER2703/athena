import { useMutation } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";

import { changePassword } from "../../api/client";
import { describeError } from "../../api/errors";
import { useAuth } from "../../auth/AuthProvider";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { TextField } from "../../shared/ui/TextField";
import {
  passwordErrorFields,
  passwordErrorMessage,
  validatePasswordChange,
} from "./account";

/**
 * Panneau Compte : se déconnecter, déconnecter partout, changer de mot de passe.
 *
 * Le mot de passe n'est jamais conservé ailleurs que dans l'état de ce
 * formulaire — jamais dans `useDraft` ni dans un stockage. « Déconnecter
 * partout » demande une confirmation et rappelle son effet : tous les
 * navigateurs du Compte, y compris celui-ci, perdent leur Ticket.
 */
export function AccountPanel() {
  const titleId = useId();
  const { session, signOut, signOutEverywhere, signingOut } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const [confirmingAll, setConfirmingAll] = useState(false);

  const change = useMutation({
    mutationFn: (input: { current: string; next: string }) =>
      changePassword(input.current, input.next),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setConfirmation("");
      setFieldErrors({});
      setChanged(true);
      setMessage(
        "Mot de passe changé. Les autres navigateurs du Compte sont déconnectés ; celui-ci reste connecté.",
      );
    },
    onError: (reason) => {
      setChanged(false);
      const fields = passwordErrorFields(reason);
      setFieldErrors(fields);
      setMessage(passwordErrorMessage(reason));
    },
  });

  const all = useMutation({
    mutationFn: () => signOutEverywhere(),
    onError: (reason) => {
      setConfirmingAll(false);
      setMessage(
        `La déconnexion globale a échoué. ${describeError(reason)}`,
      );
    },
  });

  const minLength = session?.passwordMinLength ?? null;

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setMessage(null);
    setChanged(false);
    const local = validatePasswordChange(
      { current, next, confirmation },
      minLength,
    );
    setFieldErrors(local);
    if (Object.keys(local).length > 0) {
      return;
    }
    change.mutate({ current, next });
  }

  return (
    <section aria-labelledby={titleId} className="border border-border bg-surface">
      <header className="border-b border-border px-4 py-3 sm:px-5">
        <h2
          id={titleId}
          className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
        >
          Compte
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-subtle">
          Le Compte unique de cette instance. Le Ticket Console de ce navigateur
          est le seul credential ; aucun jeton n'est lisible en JavaScript.
        </p>
      </header>

      <div className="flex flex-col gap-5 px-4 py-4 sm:px-5">
        <dl className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border pb-3">
          <dt className="font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
            Compte connecté
          </dt>
          <dd className="font-mono text-sm text-text">
            {session?.username ?? "—"}
          </dd>
        </dl>

        {message !== null ? (
          <Notice tone={changed ? "success" : "danger"}>
            <span role={changed ? "status" : "alert"}>{message}</span>
          </Notice>
        ) : null}

        <form onSubmit={submit} noValidate aria-busy={change.isPending}>
          <h3 className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted">
            Changer le mot de passe
          </h3>
          <p className="mt-1 mb-3 text-xs leading-relaxed text-subtle">
            {minLength === null
              ? "La longueur minimale est appliquée par le serveur."
              : `Le serveur impose au moins ${minLength} caractères.`}{" "}
            Les autres navigateurs du Compte seront déconnectés.
          </p>
          <div className="grid gap-3 sm:grid-cols-3">
            <TextField
              label="Mot de passe actuel"
              name="current-password"
              type="password"
              autoComplete="current-password"
              value={current}
              error={fieldErrors.current_password}
              disabled={change.isPending}
              onChange={(event) => {
                setCurrent(event.target.value);
                setMessage(null);
              }}
            />
            <TextField
              label="Nouveau mot de passe"
              name="new-password"
              type="password"
              autoComplete="new-password"
              value={next}
              error={fieldErrors.new_password}
              disabled={change.isPending}
              onChange={(event) => {
                setNext(event.target.value);
                setMessage(null);
              }}
            />
            <TextField
              label="Confirmation"
              name="new-password-confirmation"
              type="password"
              autoComplete="new-password"
              value={confirmation}
              error={fieldErrors.confirmation}
              disabled={change.isPending}
              onChange={(event) => {
                setConfirmation(event.target.value);
                setMessage(null);
              }}
            />
          </div>
          <Button
            type="submit"
            variant="primary"
            size="sm"
            className="mt-3"
            disabled={change.isPending}
          >
            {change.isPending ? "Changement…" : "Changer le mot de passe"}
          </Button>
        </form>

        <div className="border-t border-border pt-4">
          <h3 className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted">
            Navigateurs
          </h3>
          <p className="mt-1 text-xs leading-relaxed text-subtle">
            « Déconnecter partout » révoque immédiatement tous les Tickets du
            Compte, y compris celui de ce navigateur : vous serez ramené à
            l'écran de connexion. Un Ticket volé cesse alors de fonctionner.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={signingOut}
              onClick={() => {
                void signOut();
              }}
            >
              {signingOut ? "Déconnexion…" : "Se déconnecter"}
            </Button>
            {confirmingAll ? (
              <>
                <Button
                  variant="danger"
                  size="sm"
                  disabled={all.isPending}
                  onClick={() => {
                    all.mutate();
                  }}
                >
                  {all.isPending
                    ? "Déconnexion globale…"
                    : "Confirmer la déconnexion partout"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={all.isPending}
                  onClick={() => {
                    setConfirmingAll(false);
                  }}
                >
                  Annuler
                </Button>
              </>
            ) : (
              <Button
                variant="danger"
                size="sm"
                onClick={() => {
                  setMessage(null);
                  setConfirmingAll(true);
                }}
              >
                Déconnecter partout
              </Button>
            )}
          </div>
          {confirmingAll ? (
            <p role="alert" className="mt-2 text-xs leading-relaxed text-danger">
              Confirmez : tous les navigateurs, y compris celui-ci, seront
              déconnectés immédiatement.
            </p>
          ) : null}
        </div>
      </div>
    </section>
  );
}
