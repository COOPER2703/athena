import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";

import {
  describeError,
  describeFieldErrors,
} from "../../api/errors";
import { updatePlugin, type PluginEntry } from "../../api/plugins";
import { queryKeys } from "../../api/queryClient";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { TextField } from "../../shared/ui/TextField";
import {
  RESTART_NOTICE,
  configDraft,
  configPayload,
  configChanged,
  validateConfig,
} from "./plugins";

/**
 * Formulaire de configuration construit depuis le manifeste.
 *
 * Aucun champ n'est inventé : seules les clés déclarées sont rendues, avec le
 * type qu'elles annoncent. La validation locale double celle du serveur, et
 * les erreurs par champ du serveur sont traduites par `describeFieldErrors`.
 */
export function PluginConfigForm({ plugin }: { plugin: PluginEntry }) {
  const queryClient = useQueryClient();
  const fields = plugin.config;
  const signature = fields.map((field) => `${field.key}=${field.value}`).join("\u0000");
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    configDraft(fields),
  );
  const [fieldsErrors, setFieldsErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);

  // Une invalidation après écriture remplace les valeurs : le brouillon suit.
  useEffect(() => {
    setDraft(configDraft(fields));
    // `signature` résume les valeurs enregistrées, pas l'identité de l'objet.
  }, [signature]);

  const mutation = useMutation({
    mutationFn: (values: Record<string, string>) =>
      updatePlugin(plugin.name, { config: values }),
    onSuccess: async () => {
      setFieldsErrors({});
      setSaved(true);
      await queryClient.invalidateQueries({ queryKey: queryKeys.plugins });
    },
    onError: (error) => {
      setSaved(false);
      setFieldsErrors(describeFieldErrors(error));
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaved(false);
    const local = validateConfig(fields, draft);
    setFieldsErrors(local);
    if (Object.keys(local).length > 0) {
      return;
    }
    mutation.mutate(configPayload(fields, draft));
  }

  return (
    <form
      className="mt-5 border-t border-border pt-5"
      onSubmit={submit}
      noValidate
      aria-busy={mutation.isPending}
    >
      <h3 className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted">
        Configuration
      </h3>
      <div className="mt-3 grid gap-4 sm:grid-cols-2">
        {fields.map((field) => (
          <TextField
            key={field.key}
            label={field.key}
            name={`config.${field.key}`}
            value={draft[field.key] ?? ""}
            hint={
              field.type === "url"
                ? "URL http(s) complète"
                : field.required
                  ? "Champ obligatoire"
                  : "Champ facultatif"
            }
            error={fieldsErrors[field.key]}
            onChange={(event) => {
              setSaved(false);
              setDraft((current) => ({
                ...current,
                [field.key]: event.target.value,
              }));
            }}
            disabled={mutation.isPending}
          />
        ))}
      </div>

      {mutation.isError && Object.keys(fieldsErrors).length === 0 ? (
        <Notice tone="danger" className="mt-4">
          {describeError(mutation.error)}
        </Notice>
      ) : null}
      {saved && !mutation.isError ? (
        <p role="status" className="mt-3 text-xs text-accent">
          {RESTART_NOTICE}
        </p>
      ) : null}

      <Button
        type="submit"
        variant="primary"
        size="sm"
        className="mt-4"
        disabled={mutation.isPending || !configChanged(fields, draft)}
      >
        {mutation.isPending ? "Enregistrement…" : "Enregistrer la configuration"}
      </Button>
    </form>
  );
}
