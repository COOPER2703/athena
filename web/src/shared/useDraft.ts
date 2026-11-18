import { useCallback, useState } from "react";

import { readDraft, writeDraft } from "./drafts";

/**
 * Brouillon de saisie adossé au stockage de l'onglet.
 *
 * Il survit à une navigation et à un retour par l'écran de connexion, sans
 * jamais franchir l'onglet ni le serveur. Le mot de passe ne doit pas passer
 * par ce hook.
 */
export function useDraft(key: string): [string, (value: string) => void] {
  const [value, setValue] = useState<string>(() => {
    if (typeof window === "undefined") {
      return "";
    }
    return readDraft(window.sessionStorage, key);
  });

  const update = useCallback(
    (next: string) => {
      setValue(next);
      if (typeof window !== "undefined") {
        writeDraft(window.sessionStorage, key, next);
      }
    },
    [key],
  );

  return [value, update];
}
