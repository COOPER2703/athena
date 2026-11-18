import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";

import { describeError } from "../../api/errors";
import { queryKeys } from "../../api/queryClient";
import {
  createAccessToken,
  fetchTokens,
  revokeAccessToken,
  type AccessToken,
  type IssuedToken,
  type TokenKind,
} from "../../api/tokens";
import { useDraft } from "../../shared/useDraft";
import { formatDate } from "../../shared/format";
import { Badge } from "../../shared/ui/Badge";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { TextField } from "../../shared/ui/TextField";
import { CopyButton } from "./CopyButton";
import {
  SECRET_ONCE_WARNING,
  couplingCommand,
  tokenStatusLabel,
  tokenTargetLabel,
  validateTokenDraft,
} from "./tokens";

export interface AccessTokensPanelProps {
  /** URL WebSocket du transport, déjà rendue joignable depuis le navigateur. */
  transportUrl: string | null;
}

const CONTROL_CLASS =
  "h-10 w-full rounded-sm border border-border bg-canvas px-3 font-mono text-sm text-text transition-quiet hover:border-border-strong focus:border-accent disabled:cursor-not-allowed disabled:opacity-60";

/**
 * Jetons d'accès : création, secret révélé une seule fois, révocation.
 *
 * Le secret de création ne vit que dans l'état de ce panneau. Il n'est jamais
 * écrit dans un stockage, jamais renvoyé par la liste, et disparaît dès qu'on
 * le masque ou que le panneau est démonté. La commande de couplage du Client
 * desktop n'est proposée qu'à ce moment-là, quand le Jeton est encore connu.
 */
export function AccessTokensPanel({ transportUrl }: AccessTokensPanelProps) {
  const titleId = useId();
  const queryClient = useQueryClient();
  const tokens = useQuery({
    queryKey: queryKeys.tokens,
    queryFn: () => fetchTokens(),
  });

  const [name, setName] = useDraft("settings.token.name");
  const [source, setSource] = useDraft("settings.token.source");
  const [kind, setKind] = useState<TokenKind>("client");
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: (value: Parameters<typeof createAccessToken>[0]) =>
      createAccessToken(value),
    onSuccess: async (created) => {
      setIssued(created);
      setName("");
      setSource("");
      setError(null);
      setFeedback(`Jeton « ${created.token.name} » créé.`);
      await queryClient.invalidateQueries({ queryKey: queryKeys.tokens });
    },
    onError: (reason) => {
      setFeedback(null);
      setError(describeError(reason));
    },
  });

  const revoke = useMutation({
    mutationFn: (id: string) => revokeAccessToken(id),
    onSuccess: async (_result, id) => {
      const revoked = tokens.data?.find((token) => token.id === id);
      setConfirming(null);
      setError(null);
      setFeedback(
        revoked ? `Jeton « ${revoked.name} » révoqué.` : "Jeton révoqué.",
      );
      await queryClient.invalidateQueries({ queryKey: queryKeys.tokens });
    },
    onError: (reason) => {
      setError(describeError(reason));
    },
  });

  const pairing =
    issued !== null && issued.token.kind === "client" && transportUrl !== null
      ? couplingCommand(transportUrl, issued.secret)
      : null;

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setFeedback(null);
    const check = validateTokenDraft({ name, kind, source });
    if (!check.ok) {
      setError(check.message);
      return;
    }
    setError(null);
    create.mutate(check.value);
  }

  function changeKind(next: TokenKind): void {
    setKind(next);
    if (next === "client") {
      setSource("");
    }
    setError(null);
  }

  return (
    <section
      aria-labelledby={titleId}
      className="border border-border bg-surface"
    >
      <header className="border-b border-border px-4 py-3 sm:px-5">
        <h2
          id={titleId}
          className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
        >
          Jetons d'accès
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-subtle">
          Un Jeton est le credential d'une machine — Client desktop ou source du
          Webhook —, distinct du Ticket Console de ce navigateur. Il n'est
          affiché qu'une seule fois, à sa création.
        </p>
      </header>

      <div className="flex flex-col gap-5 px-4 py-4 sm:px-5">
        {issued !== null ? (
          <div className="border border-warning-line bg-warning-soft px-4 py-3">
            <p role="status" className="text-sm font-medium text-warning">
              {SECRET_ONCE_WARNING}
            </p>
            <code className="mt-3 block select-all break-all rounded-sm border border-border bg-canvas px-3 py-2 font-mono text-sm text-text">
              {issued.secret}
            </code>
            <div className="mt-3 flex flex-wrap items-start gap-2">
              <CopyButton text={issued.secret} label="Copier le secret" />
              {pairing !== null ? (
                <CopyButton text={pairing} label="Copier la commande" />
              ) : null}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setIssued(null);
                }}
              >
                Masquer
              </Button>
            </div>
            {pairing !== null ? (
              <div className="mt-3">
                <p className="text-xs leading-relaxed text-muted">
                  Commande de couplage du Client desktop :
                </p>
                <pre className="mt-1 select-all overflow-x-auto rounded-sm border border-border bg-canvas px-3 py-2 font-mono text-xs text-text">
                  <code>{pairing}</code>
                </pre>
                <p className="mt-1 text-[0.6875rem] leading-relaxed text-subtle">
                  Elle s'exécute sur la machine du Client. Si votre serveur passe
                  par un proxy, remplacez l'adresse par celle du transport.
                </p>
              </div>
            ) : issued.token.kind === "client" ? (
              <p className="mt-3 text-xs leading-relaxed text-subtle">
                L'adresse du transport n'est pas disponible : la commande de
                couplage ne peut pas être composée.
              </p>
            ) : null}
          </div>
        ) : null}

        <form onSubmit={submit} noValidate aria-busy={create.isPending}>
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1.4fr)_minmax(0,0.9fr)_minmax(0,1fr)]">
            <TextField
              label="Nom du Jeton"
              name="token-name"
              value={name}
              placeholder="Portable du bureau"
              autoComplete="off"
              spellCheck={false}
              disabled={create.isPending}
              onChange={(event) => {
                setName(event.target.value);
                setError(null);
              }}
            />
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="token-kind"
                className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
              >
                Nature
              </label>
              <select
                id="token-kind"
                name="token-kind"
                className={CONTROL_CLASS}
                value={kind}
                disabled={create.isPending}
                onChange={(event) => {
                  changeKind(event.target.value === "source" ? "source" : "client");
                }}
              >
                <option value="client">Transport client</option>
                <option value="source">Source (Webhook)</option>
              </select>
            </div>
            {kind === "source" ? (
              <TextField
                label="Source"
                name="token-source"
                value={source}
                placeholder="matrix"
                autoComplete="off"
                spellCheck={false}
                disabled={create.isPending}
                onChange={(event) => {
                  setSource(event.target.value);
                  setError(null);
                }}
              />
            ) : (
              <p className="self-end pb-2 text-xs leading-relaxed text-subtle">
                Un Jeton client authentifie le transport des Clients.
              </p>
            )}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={create.isPending}
            >
              {create.isPending ? "Création…" : "Créer le Jeton"}
            </Button>
          </div>
        </form>

        {error !== null ? (
          <Notice tone="danger">
            <span role="alert">{error}</span>
          </Notice>
        ) : null}
        {feedback !== null && error === null ? (
          <p role="status" className="text-xs leading-relaxed text-accent">
            {feedback}
          </p>
        ) : null}

        {tokens.isPending ? (
          <p role="status" className="text-sm text-muted">
            Chargement des Jetons…
          </p>
        ) : null}
        {tokens.isError ? (
          <Notice tone="danger" title="Jetons indisponibles">
            <p>{describeError(tokens.error)}</p>
            <Button
              variant="secondary"
              size="sm"
              className="mt-3"
              onClick={() => {
                void tokens.refetch();
              }}
            >
              Réessayer
            </Button>
          </Notice>
        ) : null}
        {tokens.data !== undefined && tokens.data.length === 0 ? (
          <p className="text-sm leading-relaxed text-muted">
            Aucun Jeton d'accès : les Clients et le Webhook ne peuvent pas
            s'authentifier tant qu'il n'en existe pas.
          </p>
        ) : null}

        {tokens.data !== undefined && tokens.data.length > 0 ? (
          <ul className="flex flex-col divide-y divide-border border-t border-border">
            {tokens.data.map((token) => (
              <TokenRow
                key={token.id}
                token={token}
                confirming={confirming === token.id}
                revoking={revoke.isPending && revoke.variables === token.id}
                onAskRevoke={() => {
                  setConfirming(token.id);
                  setError(null);
                }}
                onCancelRevoke={() => {
                  setConfirming(null);
                }}
                onRevoke={() => {
                  revoke.mutate(token.id);
                }}
              />
            ))}
          </ul>
        ) : null}
      </div>
    </section>
  );
}

interface TokenRowProps {
  token: AccessToken;
  confirming: boolean;
  revoking: boolean;
  onAskRevoke: () => void;
  onCancelRevoke: () => void;
  onRevoke: () => void;
}

function TokenRow({
  token,
  confirming,
  revoking,
  onAskRevoke,
  onCancelRevoke,
  onRevoke,
}: TokenRowProps) {
  return (
    <li className="py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm text-text">{token.name}</p>
            <Badge tone={token.revokedAt === null ? "accent" : "neutral"}>
              {tokenStatusLabel(token)}
            </Badge>
            <Badge>{tokenTargetLabel(token)}</Badge>
          </div>
          <p className="mt-1 break-all font-mono text-[0.6875rem] text-subtle">
            {token.prefix || "ath_…"}
          </p>
        </div>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
          <dt className="font-mono text-[0.625rem] uppercase tracking-[0.1em] text-subtle">
            Créé
          </dt>
          <dd className="font-mono text-muted">{formatDate(token.createdAt)}</dd>
          <dt className="font-mono text-[0.625rem] uppercase tracking-[0.1em] text-subtle">
            Dernière utilisation
          </dt>
          <dd className="font-mono text-muted">
            {token.lastUsedAt === null
              ? "Jamais utilisé"
              : formatDate(token.lastUsedAt)}
          </dd>
        </dl>
      </div>

      {token.revokedAt === null ? (
        confirming ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <span className="text-xs text-muted">
              Révoquer « {token.name} » ? L'accès est coupé immédiatement.
            </span>
            <Button
              type="button"
              variant="danger"
              size="sm"
              disabled={revoking}
              onClick={onRevoke}
            >
              {revoking ? "Révocation…" : "Confirmer"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={revoking}
              onClick={onCancelRevoke}
            >
              Annuler
            </Button>
          </div>
        ) : (
          <div className="mt-3">
            <Button type="button" variant="secondary" size="sm" onClick={onAskRevoke}>
              Révoquer
            </Button>
          </div>
        )
      ) : null}
    </li>
  );
}
