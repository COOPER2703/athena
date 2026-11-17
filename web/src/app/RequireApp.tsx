import { Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { screenFor } from "./routing";

/** Ne rend les routes protégées que lorsque le Ticket Console est valide. */
export function RequireApp() {
  const { session } = useAuth();
  return screenFor(session) === "app" ? <Outlet /> : null;
}
