import { useEffect, useRef, type RefObject } from "react";

/**
 * Référence qui suit toujours la dernière valeur, mise à jour dans un effet.
 *
 * Sert aux callbacks asynchrones qui ne doivent pas se rejouer à chaque rendu
 * du parent, sans écrire dans une `ref` pendant le rendu. Le délai d'un effet
 * est sans conséquence ici : les promesses se résolvent après le commit.
 */
export function useLatestRef<T>(value: T): RefObject<T> {
  const latest = useRef(value);
  useEffect(() => {
    latest.current = value;
  }, [value]);
  return latest;
}
