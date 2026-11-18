import { PluginsIcon } from "../../shared/icons";
import { EmptyState } from "../../shared/ui/EmptyState";

/**
 * Onglet « Découvrir » : la place réservée au futur store, sans installation.
 *
 * Le store — catalogue distant, signatures, provenance et politique de
 * confiance — est une décision de sécurité distincte, hors périmètre. Cet
 * onglet ne propose donc **aucun** bouton : il explique seulement ce qui
 * manque, pour qu'on ne cherche pas une installation inexistante.
 */
export function DiscoverTab() {
  return (
    <div className="flex flex-col gap-5">
      <EmptyState
        icon={<PluginsIcon className="h-5 w-5" />}
        title="Le store de Plugins n'existe pas encore"
        body="Cette place est réservée à un catalogue de Plugins à installer depuis la Console. Athena ne sait aujourd'hui ni installer ni désinstaller un Plugin : un dossier se dépose sur le serveur, puis s'active."
        hint="Aucune installation n'est possible depuis cette Console."
      />

      <section
        aria-labelledby="plugins-store-requirements"
        className="mx-auto w-full max-w-2xl rounded-sm border border-border bg-surface px-5 py-5"
      >
        <h2
          id="plugins-store-requirements"
          className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
        >
          Ce qu'un store devra garantir
        </h2>
        <ul className="mt-3 flex flex-col gap-2 text-sm leading-relaxed text-muted">
          <li className="flex gap-2">
            <span aria-hidden="true" className="text-accent">
              →
            </span>
            Une provenance vérifiable : qui a publié le Plugin, et quelle version
            exacte est installée.
          </li>
          <li className="flex gap-2">
            <span aria-hidden="true" className="text-accent">
              →
            </span>
            Une signature des paquets, pour qu'un contenu modifié en transit soit
            refusé.
          </li>
          <li className="flex gap-2">
            <span aria-hidden="true" className="text-accent">
              →
            </span>
            Une politique de confiance explicite, puisque le code s'exécute avec
            les privilèges du serveur.
          </li>
          <li className="flex gap-2">
            <span aria-hidden="true" className="text-accent">
              →
            </span>
            Une installation et une désinstallation atomiques, avec la même
            exigence de redémarrage que l'activation.
          </li>
        </ul>
      </section>
    </div>
  );
}
