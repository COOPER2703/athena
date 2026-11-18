import { describeError } from "../../api/errors";
import { ThemeToggle } from "../../design/ThemeToggle";
import { BrandMark, RefreshIcon } from "../../shared/icons";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";

export interface ServerUnavailableProps {
  error: unknown;
  onRetry: () => void;
}

/**
 * Le serveur ne répond pas : on ne propose pas un formulaire de connexion qui
 * échouerait de la même façon, mais une explication et une seule action.
 */
export function ServerUnavailable({ error, onRetry }: ServerUnavailableProps) {
  return (
    <div className="flex min-h-dvh flex-col bg-canvas">
      <header className="flex items-center justify-between px-5 py-5 sm:px-8">
        <div className="flex items-center gap-3 text-accent">
          <BrandMark className="h-6 w-6" />
          <span className="font-mono text-xs uppercase tracking-[0.28em] text-text">
            Athena
          </span>
        </div>
        <ThemeToggle />
      </header>
      <main
        id="main"
        className="flex flex-1 items-center justify-center px-5 pb-16"
      >
        <div className="w-full max-w-md rounded-sm border border-border bg-surface p-6 sm:p-8">
          <p className="font-mono text-[0.6875rem] uppercase tracking-[0.2em] text-warning">
            Connexion perdue
          </p>
          <h1 className="mt-3 text-2xl font-medium tracking-tight">
            Le serveur est injoignable
          </h1>
          <Notice tone="danger" className="mt-4">
            {describeError(error)}
          </Notice>
          <p className="mt-4 text-sm leading-relaxed text-muted">
            Vérifiez que le serveur Athena est démarré et que cette page est
            ouverte depuis son adresse habituelle.
          </p>
          <Button
            variant="secondary"
            className="mt-6 w-full"
            onClick={onRetry}
            icon={<RefreshIcon />}
          >
            Réessayer
          </Button>
        </div>
      </main>
    </div>
  );
}
