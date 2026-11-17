import { useNavigate } from "react-router-dom";

import { ArrowRightIcon, SearchIcon } from "../shared/icons";
import { Button } from "../shared/ui/Button";
import { EmptyState } from "../shared/ui/EmptyState";
import { defaultZone } from "./routing";

/** Adresse inconnue à l'intérieur de la coquille : on ne perd pas l'utilisateur. */
export function NotFound() {
  const navigate = useNavigate();
  const zone = defaultZone();

  return (
    <div className="px-5 py-8 sm:px-8 lg:px-10 lg:py-12">
      <EmptyState
        icon={<SearchIcon className="h-5 w-5" />}
        title="Cette zone n'existe pas"
        body="L'adresse demandée ne correspond à aucune zone de la Console. Le fil, les réglages, les plugins et le diagnostic restent accessibles."
        hint="Utilisez la navigation, la palette de commandes ou un raccourci g."
      >
        <Button
          variant="secondary"
          onClick={() => navigate(zone.path)}
          icon={<ArrowRightIcon />}
        >
          Aller à la {zone.label}
        </Button>
      </EmptyState>
    </div>
  );
}
