import { useEffect, useRef } from "react";

import { Badge } from "../../shared/ui/Badge";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { surfaceLabel, type SurfaceChange } from "./model";

export interface UrlConfirmDialogProps {
  open: boolean;
  changes: readonly SurfaceChange[];
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

/**
 * Confirmation explicite des nouvelles adresses de Surfaces.
 *
 * Changer un hôte ou un port coupe l'accès à la Surface concernée : le dialogue
 * montre l'adresse actuelle **et** celle qui répondra après le redémarrage.
 * `Escape` et « Annuler » n'enregistrent rien ; le focus part sur « Annuler ».
 */
export function UrlConfirmDialog({
  open,
  changes,
  busy = false,
  onCancel,
  onConfirm,
}: UrlConfirmDialogProps) {
  const dialog = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const element = dialog.current;
    if (element === null) {
      return;
    }
    if (open && !element.open) {
      element.showModal();
    } else if (!open && element.open) {
      element.close();
    }
  }, [open]);

  const consoleMoved = changes.some((change) => change.name === "console");

  return (
    <dialog
      ref={dialog}
      aria-labelledby="url-confirm-title"
      aria-describedby="url-confirm-body"
      className="m-auto w-[min(40rem,calc(100vw-2rem))] rounded-sm border border-border bg-surface p-0 text-text backdrop:bg-canvas/80"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
      onClose={() => {
        onCancel();
      }}
    >
      <div className="flex flex-col gap-4 px-5 py-5">
        <div>
          <p className="font-mono text-[0.625rem] uppercase tracking-[0.2em] text-subtle">
            Surfaces
          </p>
          <h2 id="url-confirm-title" className="mt-1 text-lg font-medium text-text">
            Confirmer la nouvelle adresse
          </h2>
        </div>

        <p id="url-confirm-body" className="text-sm leading-relaxed text-muted">
          Après le redémarrage, ces Surfaces répondront à une nouvelle adresse.
          Notez-la avant d'enregistrer : l'adresse actuelle ne sera plus servie.
        </p>

        <ul className="flex flex-col gap-3">
          {changes.map((change) => (
            <li
              key={change.name}
              className="border border-border bg-canvas px-4 py-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-text">
                  {surfaceLabel(change.name)}
                </p>
                <Badge>{change.scheme}</Badge>
              </div>
              <dl className="mt-2 grid gap-1 text-xs">
                <div className="flex flex-wrap items-baseline gap-2">
                  <dt className="text-subtle">Adresse actuelle</dt>
                  <dd className="break-all font-mono text-muted">
                    {change.currentUrl}
                  </dd>
                </div>
                <div className="flex flex-wrap items-baseline gap-2">
                  <dt className="text-subtle">Nouvelle adresse</dt>
                  <dd className="break-all font-mono text-accent">
                    {change.nextUrl}
                  </dd>
                </div>
              </dl>
            </li>
          ))}
        </ul>

        {consoleMoved ? (
          <Notice tone="warning" title="Cet onglet perdra la Console">
            La Console répondra à la nouvelle adresse. Gardez-la de côté pour
            rouvrir cette page après le redémarrage.
          </Notice>
        ) : null}

        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-4">
          <Button
            type="button"
            variant="secondary"
            onClick={onCancel}
            disabled={busy}
            autoFocus
          >
            Annuler
          </Button>
          <Button
            type="button"
            variant="primary"
            onClick={onConfirm}
            disabled={busy}
            aria-busy={busy}
          >
            {busy ? "Enregistrement…" : "Confirmer et enregistrer"}
          </Button>
        </div>
      </div>
    </dialog>
  );
}
