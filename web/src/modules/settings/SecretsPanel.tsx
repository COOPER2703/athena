import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import {
  describeError,
  describeFieldErrors,
} from "../../api/errors";
import { queryKeys } from "../../api/queryClient";
import {
  GEMINI_SECRET,
  deleteSecret,
  fetchSecrets,
  saveSecret,
  type SecretStatus,
} from "../../api/secrets";
import { useAuth } from "../../auth/AuthProvider";
import { formatDate } from "../../shared/format";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { TextField } from "../../shared/ui/TextField";
import { secretSummary } from "./model";

const GEMINI_HINT =
  "La clé Gemini est vérifiée par un appel réel avant d'être enregistrée.";

/**
 * Secrets de la Console : écriture seule, jamais relus.
 *
 * Une valeur enregistrée n'est plus jamais affichée : l'écran ne montre que
 * « Configuré le … ». Remplacer et effacer restent possibles ; une clé maître
 * perdue est expliquée, jamais compensée par une réinitialisation silencieuse.
 */
export function SecretsPanel({ unreadable }: { unreadable: boolean }) {
  const titleId = useId();
  const secrets = useQuery({
    queryKey: queryKeys.secrets,
    queryFn: () => fetchSecrets(),
  });
  const unreadableNow = unreadable || secrets.data?.unreadable === true;
  const jumped = useRef(false);

  // Le rappel de l'Installation mène à `/settings#secret-GEMINI_API_KEY` : la
  // ligne n'existe qu'après la lecture des Secrets, donc le défilement natif
  // ne suffit pas. On l'amène au clavier une fois la liste arrivée, une seule
  // fois par visite même si les Secrets sont relus.
  useEffect(() => {
    if (
      jumped.current ||
      secrets.data === undefined ||
      typeof window === "undefined"
    ) {
      return;
    }
    const hash = window.location.hash;
    if (!hash.startsWith("#secret-")) {
      return;
    }
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    if (target === null) {
      return;
    }
    jumped.current = true;
    const reduce = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    target.scrollIntoView({
      block: "center",
      behavior: reduce ? "auto" : "smooth",
    });
    target.querySelector<HTMLInputElement>("input")?.focus();
  }, [secrets.data]);

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
          Secrets
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-subtle">
          Les Secrets sont chiffrés au repos et en écriture seule : une valeur
          enregistrée n'est plus jamais affichée. Le nom est celui de la
          variable d'environnement attendue.
        </p>
      </header>

      <div className="flex flex-col gap-4 px-4 py-4 sm:px-5">
        {unreadableNow ? (
          <Notice tone="danger" title="Secrets illisibles">
            La clé maître ne déchiffre plus le magasin : les Secrets enregistrés
            ne sont pas lisibles. Rien n'a été effacé — ressaisissez-les
            ci-dessous, puis redémarrez le serveur si nécessaire.
          </Notice>
        ) : null}

        {secrets.isPending ? (
          <p role="status" className="text-sm text-muted">
            Chargement des Secrets…
          </p>
        ) : null}

        {secrets.isError ? (
          <Notice tone="danger" title="Secrets indisponibles">
            <p>{describeError(secrets.error)}</p>
            <Button
              variant="secondary"
              size="sm"
              className="mt-3"
              onClick={() => {
                void secrets.refetch();
              }}
            >
              Réessayer
            </Button>
          </Notice>
        ) : null}

        {secrets.data !== undefined && secrets.data.secrets.length === 0 ? (
          <p className="text-sm leading-relaxed text-muted">
            Aucun Secret n'est connu sur cette instance.
          </p>
        ) : null}

        <ul className="flex flex-col gap-4">
          {secrets.data?.secrets.map((secret) => (
            <SecretRow key={secret.name} secret={secret} />
          ))}
        </ul>
      </div>
    </section>
  );
}

function SecretRow({ secret }: { secret: SecretStatus }) {
  const queryClient = useQueryClient();
  const { refetch: refetchSession } = useAuth();
  const [value, setValue] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const refresh = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.secrets });
    await queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    await refetchSession();
  };

  const save = useMutation({
    mutationFn: (secretValue: string) => saveSecret(secret.name, secretValue),
    onSuccess: async () => {
      // La valeur ne sert plus : elle est chiffrée côté serveur et jamais relue.
      setValue("");
      setFieldError(null);
      setFeedback("Secret enregistré.");
      await refresh();
    },
    onError: (error) => {
      const fields = describeFieldErrors(error);
      const message = fields.value ?? fields.name ?? null;
      setFieldError(message);
      setFeedback(message === null ? describeError(error) : null);
    },
  });

  const remove = useMutation({
    mutationFn: () => deleteSecret(secret.name),
    onSuccess: async () => {
      setConfirming(false);
      setFieldError(null);
      setFeedback("Secret effacé.");
      await refresh();
    },
    onError: (error) => {
      setFeedback(describeError(error));
    },
  });

  const busy = save.isPending || remove.isPending;

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (value.trim() === "") {
      setFieldError("Saisissez une valeur.");
      setFeedback(null);
      return;
    }
    setFieldError(null);
    setFeedback(null);
    save.mutate(value);
  }

  return (
    <li
      id={`secret-${secret.name}`}
      className="border border-border bg-canvas px-4 py-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="break-all font-mono text-xs text-text">{secret.name}</p>
          <p className="mt-1 text-xs leading-relaxed text-subtle">
            {secret.configured
              ? secretSummary(secret, formatDate)
              : "Non configuré"}
          </p>
        </div>
        <span
          className={
            secret.configured
              ? "font-mono text-[0.625rem] uppercase tracking-[0.1em] text-accent"
              : "font-mono text-[0.625rem] uppercase tracking-[0.1em] text-subtle"
          }
        >
          {secret.name === GEMINI_SECRET ? "Gemini" : "Secret"}
        </span>
      </div>

      <form
        className="mt-3 flex flex-wrap items-end gap-2"
        onSubmit={submit}
        noValidate
        aria-busy={busy}
      >
        <TextField
          label={`Valeur de ${secret.name}`}
          type="password"
          name={`secret-${secret.name}`}
          autoComplete="new-password"
          spellCheck={false}
          value={value}
          hint={
            secret.name === GEMINI_SECRET
              ? GEMINI_HINT
              : "La valeur n'est jamais relue après enregistrement."
          }
          error={fieldError ?? undefined}
          disabled={busy}
          onChange={(event) => {
            setValue(event.target.value);
            setFieldError(null);
          }}
          className="min-w-[14rem] flex-1"
        />
        <Button
          type="submit"
          variant="primary"
          size="sm"
          disabled={busy}
          className="mb-6"
        >
          {save.isPending
            ? "Vérification…"
            : secret.configured
              ? "Remplacer"
              : "Enregistrer"}
        </Button>
        {secret.configured && !confirming ? (
          <Button
            type="button"
            variant="danger"
            size="sm"
            disabled={busy}
            className="mb-6"
            onClick={() => {
              setConfirming(true);
              setFeedback(null);
            }}
          >
            Effacer
          </Button>
        ) : null}
      </form>

      {confirming ? (
        <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <span className="text-xs text-muted">
            Effacer « {secret.name} » ? Une clé Gemini effacée désactive la
            Conversation jusqu'à sa saisie.
          </span>
          <Button
            type="button"
            variant="danger"
            size="sm"
            disabled={busy}
            onClick={() => {
              remove.mutate();
            }}
          >
            {remove.isPending ? "Effacement…" : "Confirmer l'effacement"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => {
              setConfirming(false);
            }}
          >
            Annuler
          </Button>
        </div>
      ) : null}

      {feedback !== null ? (
        <p
          role={save.isError || remove.isError ? "alert" : "status"}
          className={
            save.isError || remove.isError
              ? "mt-2 text-xs leading-relaxed text-danger"
              : "mt-2 text-xs leading-relaxed text-accent"
          }
        >
          {feedback}
        </p>
      ) : null}
    </li>
  );
}
