import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import {
  describeError,
  describeFieldErrors,
} from "../../api/errors";
import {
  deletePluginSecret,
  setPluginSecret,
  type PluginSecretField,
} from "../../api/plugins";
import { queryKeys } from "../../api/queryClient";
import { formatDate } from "../../shared/format";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { TextField } from "../../shared/ui/TextField";
import { secretSummary } from "./plugins";

/**
 * Secrets d'un Plugin : écriture seule, jamais relus.
 *
 * Le nom de la variable d'environnement attendue est affiché pour que la
 * valeur puisse aussi être fournie à la main au serveur. Aucune réponse ne
 * reprend la valeur ; elle n'est jamais conservée après enregistrement.
 */
export function PluginSecrets({ plugin }: { plugin: { name: string; secrets: readonly PluginSecretField[] } }) {
  return (
    <section className="mt-5 border-t border-border pt-5">
      <h3 className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted">
        Secrets
      </h3>
      <p className="mt-2 text-xs leading-relaxed text-subtle">
        Les Secrets sont en écriture seule : une valeur enregistrée n'est plus
        jamais affichée. Le nom indiqué est la variable d'environnement que le
        serveur lit en premier.
      </p>
      <ul className="mt-4 flex flex-col gap-4">
        {plugin.secrets.map((secret) => (
          <SecretRow key={secret.key} pluginName={plugin.name} secret={secret} />
        ))}
      </ul>
    </section>
  );
}

function SecretRow({
  pluginName,
  secret,
}: {
  pluginName: string;
  secret: PluginSecretField;
}) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const [fieldsErrors, setFieldsErrors] = useState<Record<string, string>>({});

  const save = useMutation({
    mutationFn: (secretValue: string) =>
      setPluginSecret(pluginName, secret.key, secretValue),
    onSuccess: async () => {
      setValue("");
      setFieldsErrors({});
      await queryClient.invalidateQueries({ queryKey: queryKeys.plugins });
    },
    onError: (error) => {
      setFieldsErrors(describeFieldErrors(error));
    },
  });

  const remove = useMutation({
    mutationFn: () => deletePluginSecret(pluginName, secret.key),
    onSuccess: async () => {
      setFieldsErrors({});
      await queryClient.invalidateQueries({ queryKey: queryKeys.plugins });
    },
  });

  const busy = save.isPending || remove.isPending;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (value.trim() === "") {
      setFieldsErrors({ value: "Saisissez une valeur." });
      return;
    }
    setFieldsErrors({});
    save.mutate(value);
  }

  return (
    <li className="rounded-sm border border-border bg-canvas px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-mono text-xs text-text">{secret.key}</p>
          <p className="mt-1 break-all font-mono text-[0.625rem] uppercase tracking-[0.1em] text-subtle">
            {secret.variable}
          </p>
        </div>
        <span className="font-mono text-[0.625rem] uppercase tracking-[0.1em] text-muted">
          {secretSummary(secret, formatDate)}
        </span>
      </div>

      <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={submit} noValidate>
        <TextField
          label={`Valeur de ${secret.key}`}
          type="password"
          name={`secret.${secret.key}`}
          autoComplete="new-password"
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
          }}
          error={fieldsErrors.value}
          disabled={busy}
          className="min-w-[14rem] flex-1"
        />
        <Button
          type="submit"
          variant="primary"
          size="sm"
          disabled={busy}
          className="mb-0.5"
        >
          {save.isPending ? "Enregistrement…" : "Enregistrer"}
        </Button>
        {secret.configured ? (
          <Button
            type="button"
            variant="danger"
            size="sm"
            disabled={busy}
            className="mb-0.5"
            onClick={() => {
              remove.mutate();
            }}
          >
            {remove.isPending ? "Effacement…" : "Effacer"}
          </Button>
        ) : null}
      </form>

      {save.isError && Object.keys(fieldsErrors).length === 0 ? (
        <Notice tone="danger" className="mt-3">
          {describeError(save.error)}
        </Notice>
      ) : null}
      {remove.isError ? (
        <Notice tone="danger" className="mt-3">
          {describeError(remove.error)}
        </Notice>
      ) : null}
    </li>
  );
}
