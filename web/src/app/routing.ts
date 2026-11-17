/**
 * Routes et zones de la Console.
 *
 * Module **pur** : il ne dépend ni du routeur ni de React. L'identité choisit
 * un écran (`screenFor`), chaque écran a une route, et les quatre zones de
 * l'application sont décrites une seule fois — navigation, palette de commandes
 * et titres lisent la même table.
 */

import type { AuthSession } from "../auth/session";

/** Écran racine de la Console, choisi uniquement depuis l'identité. */
export type AppScreen = "loading" | "installation" | "login" | "app";

/** Zones de l'application, dans l'ordre de la navigation. */
export type ZoneId = "conversation" | "settings" | "plugins" | "diagnostic";

/** Icônes disponibles pour la navigation, dessinées par la coquille. */
export type IconName = "conversation" | "settings" | "plugins" | "diagnostic";

export interface Zone {
  id: ZoneId;
  path: string;
  /** Libellé court de la navigation. */
  label: string;
  /** Titre de la page, plus explicite que le libellé. */
  title: string;
  /** Sous-titre affiché sous le titre. */
  summary: string;
  /** État vide propre à la zone. */
  emptyTitle: string;
  emptyBody: string;
  emptyHint: string;
  /** Raccourci séquence, après la touche `g`. */
  shortcut: string;
  icon: IconName;
}

/**
 * L'installation prime : tant qu'aucun Compte n'existe, seule sa création est
 * possible. Ensuite un Ticket valide ouvre l'application, sinon la connexion.
 */
export function screenFor(session: AuthSession | undefined): AppScreen {
  if (session === undefined) {
    return "loading";
  }
  if (session.setupRequired) {
    return "installation";
  }
  if (session.authenticated) {
    return "app";
  }
  return "login";
}

/** Route canonique d'un écran ; l'application ouvre sur la Conversation. */
export function pathForScreen(screen: AppScreen): string {
  switch (screen) {
    case "installation":
      return "/installation";
    case "login":
      return "/login";
    case "app":
      return "/conversation";
    case "loading":
      return "/";
  }
}

export const ZONES: readonly Zone[] = [
  {
    id: "conversation",
    path: "/conversation",
    label: "Conversation",
    title: "Conversation",
    summary: "Le fil unique d'Athena : texte, voix et Annonces.",
    emptyTitle: "Aucun échange pour l'instant",
    emptyBody:
      "Vos messages, les réponses d'Athena et les tours vocaux apparaîtront ici, dans un seul fil.",
    emptyHint: "Le son est coupé par défaut.",
    shortcut: "c",
    icon: "conversation",
  },
  {
    id: "settings",
    path: "/settings",
    label: "Réglages",
    title: "Réglages",
    summary: "Voix, modèles, mémoire et surfaces de cette instance.",
    emptyTitle: "Aucun réglage affiché",
    emptyBody:
      "Chaque valeur montrera sa source, et les changements en attente de redémarrage seront signalés ici.",
    emptyHint: "Les chemins des bases restent en lecture seule.",
    shortcut: "s",
    icon: "settings",
  },
  {
    id: "plugins",
    path: "/plugins",
    label: "Plugins",
    title: "Plugins",
    summary: "Ce qu'Athena sait faire, et avec quels privilèges.",
    emptyTitle: "Aucun Plugin à afficher",
    emptyBody:
      "L'état, la configuration et les secrets des Plugins installés apparaîtront ici.",
    emptyHint: "Le store de Plugins n'existe pas encore.",
    shortcut: "p",
    icon: "plugins",
  },
  {
    id: "diagnostic",
    path: "/diagnostic",
    label: "Diagnostic",
    title: "Diagnostic",
    summary: "Santé de l'instance, Session, Clients et Traces.",
    emptyTitle: "Aucune mesure à afficher",
    emptyBody:
      "La santé, la Session en cours, les Clients connectés et les Traces récentes apparaîtront ici.",
    emptyHint: "Les traces restent dans le journal du serveur.",
    shortcut: "d",
    icon: "diagnostic",
  },
];

/** Zone désignée par son identifiant ; `null` si elle n'existe pas. */
export function zoneById(id: string): Zone | null {
  return ZONES.find((zone) => zone.id === id) ?? null;
}

/** Zone ouverte par défaut : la Conversation. */
export function defaultZone(): Zone {
  const [first] = ZONES;
  if (first === undefined) {
    throw new Error("La Console n'a aucune zone déclarée.");
  }
  return first;
}

/** Zone qui contient ce chemin, sous-routes comprises ; `null` sinon. */
export function zoneForPath(pathname: string): Zone | null {
  const normalized =
    pathname.length > 1 && pathname.endsWith("/")
      ? pathname.slice(0, -1)
      : pathname;
  return (
    ZONES.find(
      (zone) =>
        normalized === zone.path || normalized.startsWith(`${zone.path}/`),
    ) ?? null
  );
}

/** Route demandée par le navigateur, pour y revenir après reconnexion. */
export function safeReturnPath(candidate: unknown): string | null {
  if (typeof candidate !== "string" || !candidate.startsWith("/")) {
    return null;
  }
  // Un `//hôte`, un schéma ou une barre inversée détourneraient la navigation
  // hors de la Console.
  if (candidate.startsWith("//") || candidate.includes("://")) {
    return null;
  }
  if (candidate.includes("\\")) {
    return null;
  }
  return candidate;
}
