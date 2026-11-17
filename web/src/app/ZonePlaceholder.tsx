import type { ComponentType } from "react";

import { zoneById, type ZoneId } from "./routing";
import { type IconProps } from "../shared/icons";
import { EmptyState } from "../shared/ui/EmptyState";
import { ZONE_ICONS } from "./shell/zoneIcons";

/**
 * État vide d'une zone, et rien d'autre.
 *
 * Chaque module remplace ce composant par son contenu : la coquille, la route,
 * la navigation et les raccourcis restent inchangés.
 */
export function ZonePlaceholder({ zoneId }: { zoneId: ZoneId }) {
  const zone = zoneById(zoneId);
  if (zone === null) {
    throw new Error(`Zone inconnue : ${zoneId}`);
  }
  const Icon: ComponentType<IconProps> = ZONE_ICONS[zone.icon];

  return (
    <div className="px-5 py-8 sm:px-8 lg:px-10 lg:py-12">
      <EmptyState
        icon={<Icon className="h-5 w-5" />}
        title={zone.emptyTitle}
        body={zone.emptyBody}
        hint={zone.emptyHint}
      />
    </div>
  );
}
