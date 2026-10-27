import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { createToken, listTokens, revokeToken } from "./api";
import { isUnauthorized } from "./auth";
import {
  SECRET_ONCE_WARNING,
  formatLastUsed,
  formatTokenDate,
  tokenStatusLabel,
  tokenTargetLabel,
  validateTokenDraft,
  type AccessToken,
  type IssuedToken,
  type TokenDraft,
  type TokenKind,
} from "./tokens";
import "./token-panel.css";

/** Retour d'action affiché sous le formulaire. */
type Feedback =
  | { kind: "success"; message: string }
  | { kind: "error"; message: string }
  | null;

const EMPTY_DRAFT: TokenDraft = { name: "", kind: "client", source: "" };

function errorMessage(reason: unknown): string {
  return reason instanceof Error && reason.message
    ? reason.message
    : "Impossible de joindre l'API de la Console.";
}

/**
 * Panneau des Jetons d'accès : liste, création, secret rendu une fois,
 * révocation individuelle.
 *
 * Le secret de création ne vit que dans l'état de ce composant : il n'est jamais
 * écrit dans `localStorage`/`sessionStorage`, ni renvoyé par la liste. Il est
 * effacé au démontage du panneau, et le bouton « Masquer » l'oublie plus tôt.
 */
export function TokenPanel({
  onUnauthorized,
}: {
  /** Appelé quand le Ticket Console est refusé : retour à la connexion. */
  onUnauthorized: () => void;
}) {
  const [tokens, setTokens] = useState<AccessToken[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [draft, setDraft] = useState<TokenDraft>(EMPTY_DRAFT);
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  // Une réponse arrivée après le démontage ne doit pas ressusciter un secret.
  const mounted = useRef(true);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      // Le secret rendu une seule fois ne survit pas au panneau.
      setIssued(null);
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const listed = await listTokens();
      if (!mounted.current) {
        return;
      }
      setTokens(listed);
      setLoadError(null);
    } catch (reason: unknown) {
      if (!mounted.current) {
        return;
      }
      if (isUnauthorized(reason)) {
        unauthorized.current();
        return;
      }
      setLoadError(errorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function forgetSecret() {
    setIssued(null);
    setCopied(false);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creating) {
      return;
    }
    setFeedback(null);
    const check = validateTokenDraft(draft);
    if (!check.ok) {
      setFeedback({ kind: "error", message: check.message });
      return;
    }
    setCreating(true);
    try {
      const created = await createToken(check.value);
      if (!mounted.current) {
        return;
      }
      // Le secret est affiché ici et nulle part ailleurs : la liste ne le
      // contient jamais.
      setIssued(created);
      setCopied(false);
      setDraft({ ...EMPTY_DRAFT, kind: draft.kind });
      setFeedback({
        kind: "success",
        message: `Jeton « ${created.token.name} » créé.`,
      });
      await refresh();
    } catch (reason: unknown) {
      if (!mounted.current) {
        return;
      }
      if (isUnauthorized(reason)) {
        unauthorized.current();
        return;
      }
      setFeedback({ kind: "error", message: errorMessage(reason) });
    } finally {
      if (mounted.current) {
        setCreating(false);
      }
    }
  }

  async function copySecret() {
    if (issued === null) {
      return;
    }
    try {
      await navigator.clipboard.writeText(issued.secret);
      setCopied(true);
    } catch {
      setFeedback({
        kind: "error",
        message:
          "La copie automatique a échoué : sélectionnez le secret puis copiez-le.",
      });
    }
  }

  async function handleRevoke(token: AccessToken) {
    if (revoking !== null) {
      return;
    }
    setFeedback(null);
    setRevoking(token.id);
    try {
      await revokeToken(token.id);
      if (!mounted.current) {
        return;
      }
      setConfirming(null);
      setFeedback({
        kind: "success",
        message: `Jeton « ${token.name} » révoqué.`,
      });
      await refresh();
    } catch (reason: unknown) {
      if (!mounted.current) {
        return;
      }
      if (isUnauthorized(reason)) {
        unauthorized.current();
        return;
      }
      setFeedback({ kind: "error", message: errorMessage(reason) });
    } finally {
      if (mounted.current) {
        setRevoking(null);
      }
    }
  }

  return (
    <section className="panel" aria-labelledby="tokens-title">
      <div className="panel__header">
        <h2 className="panel__title" id="tokens-title">
          Jetons d'accès
        </h2>
        {tokens !== null ? (
          <span className="badge">{tokens.length}</span>
        ) : null}
      </div>

      {issued !== null ? (
        <div className="token-secret">
          <p className="notice notice--warning" role="status">
            {SECRET_ONCE_WARNING}
          </p>
          <code className="token-secret__value">{issued.secret}</code>
          <div className="token-secret__actions">
            <button className="button" type="button" onClick={copySecret}>
              {copied ? "Copié" : "Copier le secret"}
            </button>
            <button className="button" type="button" onClick={forgetSecret}>
              Masquer
            </button>
          </div>
        </div>
      ) : null}

      <form className="token-form" onSubmit={handleSubmit}>
        <div className="token-form__fields">
          <label className="field" htmlFor="token-name">
            <span className="field__label">Nom</span>
            <input
              className="field__input"
              id="token-name"
              type="text"
              value={draft.name}
              onChange={(event) => {
                setDraft({ ...draft, name: event.target.value });
                setFeedback(null);
              }}
              placeholder="Ex. : Portable du bureau"
              autoComplete="off"
              spellCheck={false}
              disabled={creating}
            />
          </label>

          <label className="field field--kind" htmlFor="token-kind">
            <span className="field__label">Nature</span>
            <select
              className="field__input"
              id="token-kind"
              value={draft.kind}
              onChange={(event) => {
                const kind = event.target.value as TokenKind;
                setDraft({ ...draft, kind, source: kind === "client" ? "" : draft.source });
                setFeedback(null);
              }}
              disabled={creating}
            >
              <option value="client">Transport client</option>
              <option value="source">Source (Webhook)</option>
            </select>
          </label>

          {draft.kind === "source" ? (
            <label className="field" htmlFor="token-source">
              <span className="field__label">Source</span>
              <input
                className="field__input mono"
                id="token-source"
                type="text"
                value={draft.source}
                onChange={(event) => {
                  setDraft({ ...draft, source: event.target.value });
                  setFeedback(null);
                }}
                placeholder="Ex. : matrix"
                autoComplete="off"
                spellCheck={false}
                disabled={creating}
              />
            </label>
          ) : null}
        </div>

        <div className="token-form__actions">
          <button className="button" type="submit" disabled={creating}>
            {creating ? "Création…" : "Créer le Jeton"}
          </button>
        </div>

        {feedback !== null ? (
          <p
            className={
              feedback.kind === "success"
                ? "notice notice--success"
                : "notice notice--error"
            }
            role={feedback.kind === "error" ? "alert" : "status"}
          >
            {feedback.message}
          </p>
        ) : null}
      </form>

      {tokens === null && loadError === null ? (
        <p className="notice">Chargement des Jetons…</p>
      ) : null}
      {loadError !== null ? (
        <p className="notice notice--error" role="alert">
          {loadError}
        </p>
      ) : null}
      {tokens !== null && tokens.length === 0 ? (
        <p className="notice">Aucun Jeton d'accès.</p>
      ) : null}
      {tokens !== null && tokens.length > 0 ? (
        <ul className="token-list">
          {tokens.map((token) => (
            <li
              className={
                token.revokedAt === null
                  ? "token"
                  : "token token--revoked"
              }
              key={token.id}
            >
              <div className="token__identity">
                <span className="token__name">{token.name}</span>
                <code className="token__prefix">{token.prefix}</code>
                <span
                  className={
                    token.revokedAt === null
                      ? "badge"
                      : "badge badge--plugin-disabled"
                  }
                >
                  {tokenStatusLabel(token)}
                </span>
              </div>
              <dl className="token__facts">
                <div>
                  <dt>Nature</dt>
                  <dd>{tokenTargetLabel(token)}</dd>
                </div>
                <div>
                  <dt>Créé</dt>
                  <dd>{formatTokenDate(token.createdAt)}</dd>
                </div>
                <div>
                  <dt>Dernière utilisation</dt>
                  <dd>{formatLastUsed(token.lastUsedAt)}</dd>
                </div>
              </dl>

              {token.revokedAt === null ? (
                confirming === token.id ? (
                  <div className="token__confirm">
                    <span>Révoquer « {token.name} » ?</span>
                    <button
                      className="button button--danger"
                      type="button"
                      onClick={() => handleRevoke(token)}
                      disabled={revoking !== null}
                    >
                      {revoking === token.id ? "Révocation…" : "Confirmer"}
                    </button>
                    <button
                      className="button"
                      type="button"
                      onClick={() => setConfirming(null)}
                      disabled={revoking !== null}
                    >
                      Annuler
                    </button>
                  </div>
                ) : (
                  <div className="token__actions">
                    <button
                      className="button"
                      type="button"
                      onClick={() => {
                        setConfirming(token.id);
                        setFeedback(null);
                      }}
                    >
                      Révoquer
                    </button>
                  </div>
                )
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
