import { ShieldIcon } from "../../shared/icons";

/**
 * Panneau de confiance : un Plugin activé n'est pas du contenu, c'est du code.
 *
 * Il rappelle le modèle de l'ADR-0014 — code de confiance, mêmes privilèges que
 * le serveur, aucune isolation — pour que l'activation soit un consentement
 * éclairé, pas une case à cocher.
 */
export function TrustPanel() {
  return (
    <section
      aria-labelledby="plugins-trust-title"
      className="rounded-sm border border-warning-line bg-warning-soft px-5 py-4"
    >
      <div className="flex items-start gap-3">
        <ShieldIcon className="mt-0.5 h-5 w-5 shrink-0 text-warning" />
        <div className="min-w-0">
          <h2
            id="plugins-trust-title"
            className="text-sm font-medium text-warning"
          >
            Un Plugin activé tourne avec les privilèges du serveur
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-muted">
            Un Plugin est du code de confiance : il s'exécute dans le processus
            d'Athena, avec les mêmes accès que lui, sans bac à sable. N'activez
            qu'un Plugin dont vous avez lu le code ou dont vous connaissez
            l'auteur.
          </p>
          <p className="mt-2 text-xs leading-relaxed text-subtle">
            Déposer un dossier ne suffit pas : seuls les Plugins explicitement
            activés sont chargés, et une activation prend effet au redémarrage.
          </p>
        </div>
      </div>
    </section>
  );
}
