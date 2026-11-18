import { useCallback, useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { describeError } from "../../api/errors";
import { useAuth } from "../../auth/AuthProvider";
import { ThemeToggle } from "../../design/ThemeToggle";
import { useTheme } from "../../design/ThemeProvider";
import { InstallBanner } from "../../installation/InstallBanner";
import { useVoiceConversation } from "../../modules/conversation/VoiceProvider";
import {
  voiceBarVisible,
  voicePhaseLabel,
} from "../../modules/conversation/voice";
import { cn } from "../../shared/cn";
import { BrandMark, LogOutIcon, SearchIcon } from "../../shared/icons";
import { Button } from "../../shared/ui/Button";
import { Kbd } from "../../shared/ui/Kbd";
import { Notice } from "../../shared/ui/Notice";
import { ZONES, zoneById, zoneForPath, type ZoneId } from "../routing";
import { CommandPalette } from "./CommandPalette";
import type { Command } from "./palette";
import { useAppShortcuts } from "./useAppShortcuts";
import { ZONE_ICONS } from "./zoneIcons";

const NAV_BASE =
  "flex items-center gap-3 rounded-sm border border-transparent px-3 py-2 text-sm transition-quiet focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

function paletteHint(): string {
  if (typeof navigator === "undefined") {
    return "Ctrl K";
  }
  const platform =
    (
      navigator as Navigator & { userAgentData?: { platform?: string } }
    ).userAgentData?.platform ??
    navigator.platform ??
    "";
  return /mac|iphone|ipad/i.test(platform) ? "⌘K" : "Ctrl K";
}

/**
 * Coquille authentifiée : navigation à quatre zones, en-tête de zone, compte,
 * palette de commandes et raccourcis. Les modules remplissent `<Outlet />`.
 */
export function AppShell() {
  const { session, signOut, signingOut } = useAuth();
  const { theme, toggle: toggleTheme } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const zone = zoneForPath(location.pathname);
  // La Conversation occupe la hauteur disponible : le fil défile seul et la
  // barre de saisie reste au bas de l'écran. Les autres zones gardent le
  // défilement de la page.
  const conversation = zone?.id === "conversation";
  const voice = useVoiceConversation();
  const inVoice = voiceBarVisible(voice.state.phase);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const openPalette = useCallback(() => {
    setPaletteOpen(true);
  }, []);
  const closePalette = useCallback(() => {
    setPaletteOpen(false);
  }, []);

  const goToZone = useCallback(
    (id: ZoneId) => {
      const target = zoneById(id);
      if (target) {
        navigate(target.path);
      }
    },
    [navigate],
  );

  useAppShortcuts({
    enabled: !paletteOpen,
    onOpenPalette: openPalette,
    onZone: goToZone,
  });

  useEffect(() => {
    document.title =
      zone === null ? "Console · Athena" : `${zone.label} · Athena`;
  }, [zone]);

  const handleSignOut = useCallback(async () => {
    setActionError(null);
    try {
      await signOut();
    } catch (reason) {
      setActionError(describeError(reason));
    }
  }, [signOut]);

  const commands = useMemo<Command[]>(
    () => [
      ...ZONES.map((item) => ({
        id: `go-${item.id}`,
        label: `Aller à ${item.label}`,
        keywords: [item.summary, item.emptyTitle],
        shortcut: `g ${item.shortcut}`,
        group: "Navigation",
        run: () => navigate(item.path),
      })),
      {
        id: "theme",
        label: `Passer au thème ${theme === "dark" ? "clair" : "sombre"}`,
        keywords: ["apparence", "sombre", "clair", "couleur"],
        group: "Affichage",
        run: toggleTheme,
      },
      {
        id: "logout",
        label: "Se déconnecter",
        keywords: ["quitter", "fermer", "session"],
        group: "Compte",
        run: () => {
          void handleSignOut();
        },
      },
    ],
    [handleSignOut, navigate, theme, toggleTheme],
  );

  return (
    <div className="min-h-dvh bg-canvas lg:grid lg:grid-cols-[15.5rem_minmax(0,1fr)]">
      <a href="#main" className="skip-link">
        Aller au contenu
      </a>

      <aside className="flex flex-col border-b border-border bg-surface lg:sticky lg:top-0 lg:h-dvh lg:border-b-0 lg:border-r">
        <div className="flex items-center gap-3 px-5 py-4 lg:py-6">
          <BrandMark className="h-7 w-7 shrink-0 text-accent" />
          <div className="min-w-0">
            <p className="font-mono text-[0.8125rem] uppercase tracking-[0.28em] text-text">
              Athena
            </p>
            <p className="font-mono text-[0.625rem] uppercase tracking-[0.18em] text-subtle">
              Console web
            </p>
          </div>
        </div>

        <nav aria-label="Zones de la Console" className="px-3 pb-3 lg:flex-1">
          <ul className="flex gap-1 overflow-x-auto lg:flex-col">
            {ZONES.map((item) => {
              const Icon = ZONE_ICONS[item.icon];
              return (
                <li key={item.id} className="shrink-0 lg:shrink">
                  <NavLink
                    to={item.path}
                    className={({ isActive }) =>
                      cn(
                        NAV_BASE,
                        isActive
                          ? "halo-active border-accent-line bg-accent-soft text-accent"
                          : "text-muted hover:bg-surface-2 hover:text-text",
                      )
                    }
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="flex-1">{item.label}</span>
                    <Kbd className="hidden bg-canvas/40 lg:inline-flex">
                      g {item.shortcut}
                    </Kbd>
                  </NavLink>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="hidden border-t border-border p-3 lg:block">
          <ThemeToggle />
          <p className="mt-3 px-2 font-mono text-[0.625rem] leading-relaxed text-subtle">
            g puis c/s/p/d pour changer de zone.
          </p>
        </div>
      </aside>

      <div
        className={cn(
          "flex min-h-0 min-w-0 flex-col",
          conversation ? "lg:h-dvh" : "lg:min-h-dvh",
        )}
      >
        <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border px-5 py-4 sm:px-8 lg:px-10">
          <div className="min-w-0">
            <p className="font-mono text-[0.625rem] uppercase tracking-[0.2em] text-subtle">
              {zone?.label ?? "Console"}
            </p>
            <h1 className="truncate text-lg font-medium tracking-tight text-text">
              {zone?.title ?? "Zone inconnue"}
            </h1>
            <p className="truncate text-xs text-muted">
              {zone?.summary ?? "Cette adresse ne correspond à aucune zone."}
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {inVoice ? (
              <button
                type="button"
                onClick={() =>
                  navigate(zoneById("conversation")?.path ?? "/conversation")
                }
                title={`Conversation vocale : ${voicePhaseLabel(
                  voice.state.phase,
                )}`}
                className="inline-flex items-center gap-2 rounded-sm border border-accent-line bg-accent-soft px-2.5 py-1.5 font-mono text-[0.625rem] uppercase tracking-[0.12em] text-accent transition-quiet hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
              >
                <span
                  aria-hidden="true"
                  className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent"
                />
                en conversation
              </button>
            ) : null}
            <span
              className="hidden items-center gap-2 rounded-sm border border-border px-2.5 py-1.5 font-mono text-[0.625rem] uppercase tracking-[0.12em] text-muted sm:inline-flex"
              title="Le Ticket Console de ce navigateur est valide."
            >
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 rounded-full bg-accent"
              />
              Ticket actif
            </span>
            <Button
              variant="secondary"
              size="sm"
              onClick={openPalette}
              aria-label="Ouvrir la palette de commandes"
              icon={<SearchIcon />}
            >
              <span className="hidden sm:inline">Commandes</span>
              <Kbd className="ml-1 hidden sm:inline-flex">
                {paletteHint()}
              </Kbd>
            </Button>
            <div className="lg:hidden">
              <ThemeToggle />
            </div>
          </div>
        </header>

        <InstallBanner />

        {actionError ? (
          <div className="px-5 pt-4 sm:px-8 lg:px-10">
            <Notice tone="danger">
              <div className="flex items-start justify-between gap-4">
                <span>{actionError}</span>
                <button
                  type="button"
                  onClick={() => setActionError(null)}
                  className="shrink-0 font-mono text-xs underline decoration-dotted underline-offset-4 hover:text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
                >
                  Fermer
                </button>
              </div>
            </Notice>
          </div>
        ) : null}

        <main
          id="main"
          tabIndex={-1}
          className={cn(
            "flex-1 focus:outline-none",
            conversation && "lg:flex lg:min-h-0 lg:flex-col lg:overflow-hidden",
          )}
        >
          <Outlet />
        </main>

        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-5 py-3 sm:px-8 lg:px-10">
          <p className="hidden font-mono text-[0.625rem] text-subtle md:block">
            g c Conversation · g s Réglages · g p Plugins · g d Diagnostic
          </p>
          <div className="flex items-center gap-3">
            <span className="font-mono text-[0.6875rem] text-muted">
              {session?.username ?? "Compte"}
            </span>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                void handleSignOut();
              }}
              disabled={signingOut}
              icon={<LogOutIcon />}
            >
              {signingOut ? "Déconnexion…" : "Se déconnecter"}
            </Button>
          </div>
        </footer>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={closePalette}
        commands={commands}
      />
    </div>
  );
}
