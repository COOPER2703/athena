import { describeError } from "../api/errors";
import { RefreshIcon, ShieldIcon } from "../shared/icons";
import { Badge } from "../shared/ui/Badge";
import { Button } from "../shared/ui/Button";
import { Notice } from "../shared/ui/Notice";
import { useAuth } from "./AuthProvider";
import { AuthLayout } from "./AuthLayout";

const STEPS: ReadonlyArray<[string, string]> = [
  ["01", "Créer l'unique Compte de cette instance."],
  [
    "02",
    "Enregistrer une clé Gemini utilisable — cette étape peut être passée.",
  ],
  ["03", "Régler la voix et le profil, puis vérifier ce qui reste à faire."],
];

/**
 * Installation : coquille de l'écran guidé.
 *
 * Le parcours complet — Compte, clé, voix, rappel permanent — est construit
 * par le ticket d'installation ; cette coquille tient la place et la route,
 * sans jamais demander de Code d'installation.
 */
export function InstallationScreen() {
  const { refetch, error } = useAuth();

  return (
    <AuthLayout
      eyebrow="Installation"
      title="Athena n'est pas encore installée"
      summary="Aucun Compte n'existe sur cette instance. L'installation se fait entièrement depuis cette Console, sans terminal."
      footer="Aucun Code d'installation n'est demandé : le premier visiteur crée l'unique Compte, puis l'installation se ferme pour de bon."
    >
      {error ? (
        <Notice tone="danger" className="mb-5">
          {describeError(error)}
        </Notice>
      ) : null}

      <ol className="flex flex-col gap-3">
        {STEPS.map(([number, label]) => (
          <li key={number} className="flex items-start gap-3">
            <span className="mt-0.5 font-mono text-[0.6875rem] tracking-[0.12em] text-accent">
              {number}
            </span>
            <span className="text-sm leading-relaxed text-muted">{label}</span>
          </li>
        ))}
      </ol>

      <div className="mt-6 flex flex-wrap items-center gap-2">
        <Badge tone="accent">Écran guidé à venir</Badge>
        <Badge>
          <ShieldIcon className="h-3 w-3" />
          Instance non exposée
        </Badge>
      </div>

      <Button
        variant="secondary"
        className="mt-6 w-full"
        onClick={refetch}
        icon={<RefreshIcon />}
      >
        Vérifier à nouveau
      </Button>
    </AuthLayout>
  );
}
