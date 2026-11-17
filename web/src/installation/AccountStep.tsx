import { useState, type FormEvent } from "react";

import { describeError } from "../api/errors";
import { useAuth } from "../auth/AuthProvider";
import { useDraft } from "../shared/useDraft";
import { Button } from "../shared/ui/Button";
import { Notice } from "../shared/ui/Notice";
import { TextField } from "../shared/ui/TextField";
import { accountErrorFields, validateAccount } from "./installation";

export interface AccountStepProps {
  /** Longueur minimale annoncée par le serveur, `null` s'il ne la donne pas. */
  minLength: number | null;
  onInstalled: (masterKey: string | null) => void;
}

/**
 * Premier écran : l'unique Compte de l'instance.
 *
 * La longueur minimale du mot de passe est expliquée **avant** la saisie et
 * vient du serveur ; le front ne fait qu'anticiper son refus. Le nom saisi
 * survit à une navigation (`useDraft`) ; les mots de passe, jamais.
 */
export function AccountStep({ minLength, onInstalled }: AccountStepProps) {
  const { installAccount, installing } = useAuth();
  const [username, setUsername] = useDraft("install.username");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  const rule =
    minLength === null
      ? "Le serveur applique sa propre longueur minimale et refusera un mot de passe trop court."
      : `Le mot de passe doit contenir au moins ${minLength} caractères : cette longueur est appliquée par le serveur.`;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const local = validateAccount({ username, password, confirmation }, minLength);
    setFields(local);
    if (Object.keys(local).length > 0) {
      return;
    }

    try {
      const masterKey = await installAccount({
        username: username.trim(),
        password,
      });
      setPassword("");
      setConfirmation("");
      setUsername("");
      onInstalled(masterKey);
    } catch (reason) {
      const mapped = accountErrorFields(reason);
      setFields(mapped);
      setError(Object.keys(mapped).length > 0 ? null : describeError(reason));
    }
  }

  return (
    <>
      <p className="mb-5 text-sm leading-relaxed text-muted">{rule}</p>

      {error ? (
        <Notice tone="danger" className="mb-5">
          {error}
        </Notice>
      ) : null}

      <form onSubmit={submit} noValidate aria-busy={installing}>
        <div className="flex flex-col gap-4">
          <TextField
            label="Nom du Compte"
            name="username"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            error={fields.username}
            disabled={installing}
          />
          <TextField
            label="Mot de passe"
            name="password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            hint={minLength === null ? undefined : `Au moins ${minLength} caractères.`}
            error={fields.password}
            disabled={installing}
          />
          <TextField
            label="Confirmation du mot de passe"
            name="confirmation"
            type="password"
            autoComplete="new-password"
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
            error={fields.confirmation}
            disabled={installing}
          />
        </div>
        <Button
          type="submit"
          variant="primary"
          className="mt-6 w-full"
          disabled={installing}
        >
          {installing ? "Création…" : "Créer le Compte"}
        </Button>
      </form>
    </>
  );
}
