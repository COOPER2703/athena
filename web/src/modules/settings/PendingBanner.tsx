import type { SettingsPayload } from "../../api/settings";
import { pluralize } from "../../shared/format";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { CopyButton } from "./CopyButton";
import {
  consoleUrlAfterRestart,
  displayValue,
  pendingFields,
  pendingSummary,
  pendingValue,
  settingLabel,
  type RestartPlan,
} from "./model";
import type { RecoveryPhase } from "./useRestartRecovery";

export interface PendingBannerProps {
  payload: SettingsPayload;
  plan: RestartPlan;
  phase: RecoveryPhase;
  verifying: boolean;
  onVerify: () => void;
}

/**
 * Bandeau des changements enregistrés qui attendent un redémarrage.
 *
 * Il dit combien de champs changent, lesquels, et comment le serveur revient :
 * seul (attente), par une commande à exécuter, ou sur une autre adresse. Rien
 * n'est présenté comme actif avant le redémarrage.
 */
export function PendingBanner({
  payload,
  plan,
  phase,
  verifying,
  onVerify,
}: PendingBannerProps) {
  const fields = pendingFields(payload);
  if (fields.length === 0 && plan.kind === "none") {
    return null;
  }

  const summary =
    pendingSummary(payload) ||
    `${pluralize(fields.length, "changement")}, redémarrage requis`;
  const movedConsoleUrl = consoleUrlAfterRestart(payload);

  return (
    <Notice tone="warning" title={summary}>
      <div className="flex flex-col gap-4">
        {fields.length > 0 ? (
          <ul className="flex flex-col gap-1">
            {fields.map((field) => (
              <li key={field.key} className="text-xs">
                <span className="text-text">{settingLabel(field.key)}</span>
                <span className="text-subtle"> — effectif </span>
                <code className="font-mono text-muted">
                  {displayValue(field)}
                </code>
                <span className="text-subtle">, après redémarrage </span>
                <code className="font-mono text-text">
                  {displayValue({ ...field, value: pendingValue(field) })}
                </code>
                {field.pendingValue === null ? (
                  <span className="text-subtle"> (défaut)</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}

        {plan.kind === "manual" ? (
          <div>
            <p className="text-xs leading-relaxed">
              Le redémarrage automatique est désactivé sur cette instance.
              Exécutez la commande suivante sur l'hôte du serveur :
            </p>
            <pre className="mt-2 overflow-x-auto rounded-sm border border-border bg-canvas px-3 py-2 font-mono text-xs text-text">
              <code>{plan.command || "Redémarrez le serveur depuis votre superviseur."}</code>
            </pre>
            {plan.command ? (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <CopyButton text={plan.command} label="Copier la commande" />
              </div>
            ) : null}
            {movedConsoleUrl !== null ? (
              <p className="mt-2 text-xs leading-relaxed">
                Après le redémarrage, la Console répondra à{" "}
                <code className="break-all font-mono text-text">
                  {movedConsoleUrl}
                </code>
                . Gardez cette adresse pour rouvrir cette page.
              </p>
            ) : null}
            <div className="mt-3">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={onVerify}
                disabled={verifying}
                aria-busy={verifying}
              >
                {verifying ? "Vérification…" : "J'ai redémarré, vérifier"}
              </Button>
            </div>
          </div>
        ) : null}

        {plan.kind === "moved" ? (
          <div>
            <p className="text-xs leading-relaxed">
              La Console répondra désormais à{" "}
              <code className="break-all font-mono text-text">{plan.url}</code>.
              Cet onglet ne pourra plus la joindre : ouvrez la nouvelle adresse
              après le redémarrage.
            </p>
            <p className="mt-2">
              <a
                href={plan.url}
                className="break-all font-mono text-xs text-accent underline decoration-dotted underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
              >
                Ouvrir {plan.url}
              </a>
            </p>
          </div>
        ) : null}

        {plan.kind === "poll" ? (
          <div className="flex flex-col gap-2">
            {phase === "waiting" ? (
              <p role="status" className="text-xs leading-relaxed text-text">
                Redémarrage du serveur… La Console se reconnectera toute seule.
              </p>
            ) : null}
            {/* Le retour du serveur est confirmé par l'avis de la zone, qui
                reste après la disparition de ce bandeau : le répéter ici
                afficherait deux fois la même phrase. */}
            {phase === "timeout" ? (
              <p role="alert" className="text-xs leading-relaxed text-danger">
                Le serveur n'a pas répondu dans le délai imparti. Vérifiez son
                état, puis retentez la connexion.
              </p>
            ) : null}
            {phase === "idle" || phase === "timeout" ? (
              <div>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={onVerify}
                  disabled={verifying}
                  aria-busy={verifying}
                >
                  {verifying ? "Vérification…" : "Vérifier la connexion"}
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </Notice>
  );
}
