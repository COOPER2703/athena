import { Badge } from "../shared/ui/Badge";
import { Button } from "../shared/ui/Button";

export interface VoiceStepProps {
  /** Nom du Compte, repris comme profil par défaut. */
  username: string | null;
  onDone: () => void;
}

/**
 * Aperçu des valeurs par défaut de voix et de profil.
 *
 * Rien n'est enregistré ici : les valeurs fines — voix, modèles, mémoire,
 * surfaces — se modifient dans les Réglages. Cette étape ne fait que montrer
 * avec quoi Athena démarrera, et se passe sans rien changer.
 */
export function VoiceStep({ username, onDone }: VoiceStepProps) {
  // Ces valeurs reprennent les défauts de `server/core/settings.py`. Elles
  // seront lues depuis `GET /api/settings` quand les Réglages existeront ; d'ici
  // là, cet écran ne fait que montrer l'aperçu et n'écrit rien.
  const rows: ReadonlyArray<[string, string]> = [
    ["Voix", "Leda (voix par défaut d'Athena)"],
    ["Modèle Live", "gemini-3.1-flash-live-preview"],
    ["Modèle du juge", "gemini-3.1-flash-lite"],
    ["Profil", username ?? "owner"],
  ];

  return (
    <>
      <p className="mb-5 text-sm leading-relaxed text-muted">
        Voici les valeurs par défaut de cette instance. Vous pourrez les
        ajuster dans les Réglages ; rien n'est à saisir ici.
      </p>

      <dl className="divide-y divide-border rounded-sm border border-border bg-canvas">
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3"
          >
            <dt className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted">
              {label}
            </dt>
            <dd className="font-mono text-sm text-text">{value}</dd>
          </div>
        ))}
      </dl>

      <div className="mt-3">
        <Badge>Valeurs par défaut</Badge>
      </div>

      <Button
        variant="primary"
        className="mt-6 w-full"
        autoFocus
        onClick={onDone}
      >
        Continuer
      </Button>
    </>
  );
}
