import type { SettingSource } from "../../api/settings";
import { Badge } from "../../shared/ui/Badge";
import { sourceLabel, sourceTone } from "./model";

/**
 * Badge de provenance d'une valeur effective.
 *
 * Le libellé français est affiché ; la valeur brute (`default`, `store`, `env`)
 * reste dans l'infobulle pour que le vocabulaire du serveur ne soit pas perdu.
 */
export function SourceBadge({ source }: { source: SettingSource }) {
  const label = sourceLabel(source);
  return (
    <span title={`Source de la valeur : ${label} (${source})`}>
      <Badge tone={sourceTone(source)}>{label}</Badge>
    </span>
  );
}
