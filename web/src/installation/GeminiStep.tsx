import { useState, type FormEvent } from "react";

import { describeError } from "../api/errors";
import { GEMINI_SECRET, saveSecret } from "../api/secrets";
import { useAuth } from "../auth/AuthProvider";
import { Button } from "../shared/ui/Button";
import { Notice } from "../shared/ui/Notice";
import { TextField } from "../shared/ui/TextField";
import { geminiFieldError } from "./installation";

export interface GeminiStepProps {
  /** Appelé après l'enregistrement réussi, ou quand l'étape est passée. */
  onDone: () => void;
}

/**
 * Clé Gemini : validée par un appel réel, jamais réaffichée.
 *
 * L'étape est passable — l'Installation continue, mais la Conversation reste
 * désactivée et un rappel permanent conduit au réglage manquant.
 */
export function GeminiStep({ onDone }: GeminiStepProps) {
  const { refetch } = useAuth();
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (value.trim() === "") {
      setFieldError("Collez la clé Gemini, ou passez cette étape.");
      return;
    }
    setFieldError(null);
    setSaving(true);
    try {
      await saveSecret(GEMINI_SECRET, value.trim());
      // La valeur n'est plus utile une fois chiffrée par le serveur.
      setValue("");
      await refetch();
      onDone();
    } catch (reason) {
      const message = geminiFieldError(reason);
      setFieldError(message);
      setError(message === null ? describeError(reason) : null);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <p className="mb-5 text-sm leading-relaxed text-muted">
        Athena a besoin d'une clé Gemini pour parler. Elle est vérifiée par un
        appel réel avant d'être enregistrée, puis elle n'est plus jamais
        affichée. Vous pouvez passer cette étape et la saisir plus tard dans les
        Réglages.
      </p>

      {error ? (
        <Notice tone="danger" className="mb-5">
          {error}
        </Notice>
      ) : null}

      <form onSubmit={submit} noValidate aria-busy={saving}>
        <TextField
          label="Clé Gemini"
          name="gemini"
          type="password"
          autoComplete="off"
          autoFocus
          spellCheck={false}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          hint="Collez la clé telle que fournie par Google AI Studio."
          error={fieldError ?? undefined}
          disabled={saving}
        />
        <Button
          type="submit"
          variant="primary"
          className="mt-6 w-full"
          disabled={saving}
        >
          {saving ? "Vérification…" : "Enregistrer la clé"}
        </Button>
      </form>

      <Button
        variant="ghost"
        className="mt-2 w-full"
        onClick={onDone}
        disabled={saving}
      >
        Passer cette étape
      </Button>

      <p className="mt-4 border-t border-border pt-4 text-xs leading-relaxed text-subtle">
        Sans clé, le transport des Clients, le Webhook et Gemini restent fermés,
        et la Conversation est désactivée. Un rappel restera visible jusqu'à ce
        qu'une clé utilisable soit enregistrée.
      </p>
    </>
  );
}
