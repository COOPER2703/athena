import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState, type FormEvent } from "react";

import { describeError, describeFieldErrors } from "../api/errors";
import { queryKeys } from "../api/queryClient";
import {
  fetchSettings,
  saveSettings,
  type SettingField,
} from "../api/settings";
import { settingsUnavailable } from "../modules/settings/model";
import { Button } from "../shared/ui/Button";
import { Notice } from "../shared/ui/Notice";
import { TextField } from "../shared/ui/TextField";
import {
  voiceDraft,
  voiceErrors,
  voiceFields,
  voicePayload,
} from "./voice";

/** Voix Gemini proposées ; la valeur courante est toujours ajoutée si absente. */
const VOICE_CHOICES = [
  "Leda",
  "Puck",
  "Charon",
  "Kore",
  "Fenrir",
  "Aoede",
  "Zephyr",
] as const;

const CONTROL_CLASS =
  "h-10 w-full rounded-sm border border-border bg-canvas px-3 font-mono text-sm text-text transition-quiet hover:border-border-strong focus:border-accent disabled:cursor-not-allowed disabled:opacity-60";

const MODEL_LABELS: Readonly<Record<string, string>> = {
  live_model: "Modèle Live",
  judge_model: "Modèle du juge",
};

export interface VoiceStepProps {
  /** Nom du Compte, seul « profil » réel de cette étape. */
  username: string | null;
  onDone: () => void;
}

/**
 * Voix et profil : valeurs **effectives** du serveur, pré-remplies et passables.
 *
 * La voix et les modèles viennent de `GET /api/settings` ; l'enregistrement
 * envoie l'objet complet qu'exige `PUT /api/settings`, sans toucher aux hôtes
 * ni aux ports. Un magasin indisponible est dit franchement : l'étape se passe
 * sans inventer de valeur. Le profil n'est que le nom du Compte, affiché comme
 * tel — ce n'est pas le profil de mémoire.
 */
export function VoiceStep({ username, onDone }: VoiceStepProps) {
  const titleId = useId();
  const settings = useQuery({
    queryKey: queryKeys.settings,
    queryFn: () => fetchSettings(),
  });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);

  const fields: readonly SettingField[] = settings.data?.fields ?? [];
  const signature = fields
    .map((field) => `${field.key}=${field.value}|${field.readOnly}`)
    .join("\u0000");

  useEffect(() => {
    if (settings.data === undefined) {
      return;
    }
    setDraft(voiceDraft(settings.data.fields));
    setErrors({});
  }, [signature]);

  const save = useMutation({
    mutationFn: () => saveSettings(voicePayload(fields, draft)),
    onSuccess: () => {
      setMessage(null);
      onDone();
    },
    onError: (reason) => {
      const fieldErrors = describeFieldErrors(reason);
      setErrors(fieldErrors);
      setMessage(
        Object.keys(fieldErrors).length > 0
          ? "Ces valeurs sont refusées : corrigez-les ou passez l'étape."
          : describeError(reason),
      );
    },
  });

  const unavailable =
    settings.isError && settingsUnavailable(settings.error);
  const editable = voiceFields(fields);
  const voice = editable.find((field) => field.key === "voice");
  const models = editable.filter((field) => field.key !== "voice");
  const voiceValue = voice === undefined ? "" : (draft.voice ?? voice.value);
  const voiceOptions = VOICE_CHOICES.includes(
    voiceValue as (typeof VOICE_CHOICES)[number],
  )
    ? [...VOICE_CHOICES]
    : [voiceValue, ...VOICE_CHOICES].filter((item) => item !== "");

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setMessage(null);
    const local = voiceErrors(fields, draft);
    setErrors(local);
    if (Object.keys(local).length > 0) {
      setMessage("Corrigez les champs signalés, puis réessayez.");
      return;
    }
    save.mutate();
  }

  return (
    <>
      <p className="mb-5 text-sm leading-relaxed text-muted">
        Voici les valeurs que cette instance utilise réellement. Choisissez la
        voix d'Athena puis, si vous le souhaitez, ses modèles. Vous pourrez tout
        ajuster plus tard dans les Réglages ; cette étape est passable.
      </p>

      {settings.isPending ? (
        <Notice tone="info" title="Lecture des réglages">
          La Console interroge la configuration effective du serveur…
        </Notice>
      ) : null}

      {unavailable ? (
        <Notice tone="warning" title="Réglages indisponibles">
          <p>{describeError(settings.error)}</p>
          <p className="mt-2 text-xs">
            La voix et les modèles ne peuvent pas être choisis ici pour
            l'instant. Vous pouvez passer cette étape sans rien inventer :
            l'Installation continue avec les valeurs par défaut du serveur.
          </p>
        </Notice>
      ) : null}

      {settings.isError && !unavailable ? (
        <Notice tone="danger" title="Réglages illisibles">
          <p>{describeError(settings.error)}</p>
          <p className="mt-2 text-xs">
            Vous pouvez passer cette étape ; les valeurs restent celles du
            serveur.
          </p>
        </Notice>
      ) : null}

      {settings.data !== undefined ? (
        <form onSubmit={submit} noValidate aria-busy={save.isPending}>
          <section aria-labelledby={titleId} className="flex flex-col gap-4">
            <h2
              id={titleId}
              className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
            >
              Voix et modèles
            </h2>

            {voice !== undefined ? (
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="installation-voice"
                  className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
                >
                  Voix
                </label>
                <select
                  id="installation-voice"
                  name="voice"
                  className={CONTROL_CLASS}
                  value={voiceValue}
                  disabled={save.isPending}
                  onChange={(event) => {
                    setMessage(null);
                    setDraft((current) => ({
                      ...current,
                      voice: event.target.value,
                    }));
                  }}
                >
                  {voiceOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
                <p className="text-xs leading-relaxed text-subtle">
                  Nom de la voix Gemini utilisée par Athena.
                </p>
              </div>
            ) : null}

            <div className="grid gap-4 sm:grid-cols-2">
              {models.map((field) => (
                <TextField
                  key={field.key}
                  label={MODEL_LABELS[field.key] ?? field.key}
                  name={field.key}
                  value={draft[field.key] ?? field.value}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={save.isPending}
                  error={errors[field.key]}
                  onChange={(event) => {
                    setMessage(null);
                    setDraft((current) => ({
                      ...current,
                      [field.key]: event.target.value,
                    }));
                  }}
                />
              ))}
            </div>
          </section>

          <dl className="mt-5 divide-y divide-border rounded-sm border border-border bg-canvas">
            <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3">
              <dt className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted">
                Compte
              </dt>
              <dd className="font-mono text-sm text-text">
                {username ?? "—"}
              </dd>
            </div>
          </dl>
          <p className="mt-2 text-xs leading-relaxed text-subtle">
            Le profil de mémoire se règle séparément dans les Réglages ; ici, il
            s'agit seulement du nom du Compte connecté.
          </p>

          {message !== null ? (
            <Notice tone="danger" className="mt-4">
              <span role="alert">{message}</span>
            </Notice>
          ) : null}

          <Button
            type="submit"
            variant="primary"
            className="mt-6 w-full"
            disabled={save.isPending || editable.length === 0}
          >
            {save.isPending ? "Enregistrement…" : "Enregistrer la voix"}
          </Button>
        </form>
      ) : null}

      <Button
        variant="ghost"
        className="mt-2 w-full"
        autoFocus={settings.isPending}
        onClick={onDone}
        disabled={save.isPending}
      >
        {settings.data === undefined
          ? "Continuer"
          : "Passer cette étape"}
      </Button>
    </>
  );
}
