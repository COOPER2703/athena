import { useEffect, useRef } from "react";

import type { ZoneId } from "../routing";
import {
  isEditableTarget,
  isPaletteShortcut,
  reduceShortcut,
} from "./shortcuts";

export interface AppShortcutsOptions {
  /** Palette ouverte : la coquille laisse le clavier à la palette. */
  enabled: boolean;
  onOpenPalette: () => void;
  onZone: (zone: ZoneId) => void;
}

/**
 * Raccourcis globaux : `Cmd/Ctrl+K` ouvre la palette, `g` puis `c/s/p/d`
 * change de zone. Une frappe dans un champ n'est jamais détournée.
 */
export function useAppShortcuts({
  enabled,
  onOpenPalette,
  onZone,
}: AppShortcutsOptions): void {
  const chord = useRef(false);

  useEffect(() => {
    if (!enabled) {
      chord.current = false;
      return;
    }

    function handle(event: globalThis.KeyboardEvent): void {
      if (event.defaultPrevented) {
        return;
      }
      if (isPaletteShortcut(event)) {
        event.preventDefault();
        onOpenPalette();
        return;
      }
      if (event.altKey || event.metaKey || event.ctrlKey) {
        return;
      }
      if (isEditableTarget(event.target as HTMLElement | null)) {
        chord.current = false;
        return;
      }

      const result = reduceShortcut({ chord: chord.current }, event.key);
      chord.current = result.state.chord;
      if (result.zone !== undefined) {
        event.preventDefault();
        onZone(result.zone);
      } else if (result.state.chord) {
        // `g` seul n'a pas d'action navigateur : on l'absorbe le temps de la
        // séquence.
        event.preventDefault();
      }
    }

    window.addEventListener("keydown", handle);
    return () => {
      window.removeEventListener("keydown", handle);
    };
  }, [enabled, onOpenPalette, onZone]);
}
