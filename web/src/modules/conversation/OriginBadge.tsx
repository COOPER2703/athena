import type { ThreadOrigin } from "../../api/thread";
import { Badge } from "../../shared/ui/Badge";
import { originLabel, originTone } from "./origin";

/** Badge d'origine d'un tour : clavier, voix ou Annonce. */
export function OriginBadge({ origin }: { origin: ThreadOrigin }) {
  return <Badge tone={originTone(origin)}>{originLabel(origin)}</Badge>;
}
