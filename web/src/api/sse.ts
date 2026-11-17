/**
 * Analyseur de flux SSE, sans DOM ni `fetch`.
 *
 * Le serveur décrit un Tour avec des trames `text`, `audio`, `tool`, `done`,
 * `error` et `busy`. L'analyseur est incrémental : il accepte des morceaux
 * coupés n'importe où, y compris entre `\r` et `\n`, et ne rend un événement
 * qu'une fois son bloc terminé par une ligne vide.
 */

/** Événement SSE complet, prêt à être réduit par l'écran appelant. */
export interface SseEvent {
  event: string;
  data: string;
  id?: string;
  retry?: number;
}

export interface SseParser {
  /** Consomme un morceau de texte et rend les événements qu'il termine. */
  push(chunk: string): SseEvent[];
  /** Termine le flux : un dernier événement incomplet est tout de même rendu. */
  flush(): SseEvent[];
}

export function createSseParser(): SseParser {
  let buffer = "";
  // Un `\r` final peut être le début d'un `\r\n` coupé entre deux morceaux :
  // il attend le morceau suivant au lieu de compter comme une fin de ligne.
  let pendingCarriageReturn = "";
  let started = false;

  let event = "";
  let data = "";
  let hasData = false;
  let id: string | undefined;
  let retry: number | undefined;

  function reset(): void {
    event = "";
    data = "";
    hasData = false;
    id = undefined;
    retry = undefined;
  }

  function dispatch(out: SseEvent[]): void {
    if (!hasData) {
      reset();
      return;
    }
    out.push({
      event: event || "message",
      data,
      ...(id === undefined ? {} : { id }),
      ...(retry === undefined ? {} : { retry }),
    });
    reset();
  }

  function processLine(line: string, out: SseEvent[]): void {
    if (line === "") {
      dispatch(out);
      return;
    }
    if (line.startsWith(":")) {
      return;
    }
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    const rawValue = separator === -1 ? "" : line.slice(separator + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;

    switch (field) {
      case "event":
        event = value;
        return;
      case "data":
        data = hasData ? `${data}\n${value}` : value;
        hasData = true;
        return;
      case "id":
        id = value;
        return;
      case "retry":
        if (/^\d+$/.test(value)) {
          retry = Number(value);
        }
        return;
      default:
        // Champ inconnu : ignoré, le flux reste lisible par une version future.
        return;
    }
  }

  function normalize(value: string): string {
    return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  }

  return {
    push(chunk: string): SseEvent[] {
      if (!started) {
        started = true;
        chunk = chunk.replace(/^\uFEFF/, "");
      }
      buffer += chunk;
      if (buffer.endsWith("\r")) {
        pendingCarriageReturn = "\r";
        buffer = buffer.slice(0, -1);
      } else {
        pendingCarriageReturn = "";
      }
      buffer = normalize(buffer);
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      buffer += pendingCarriageReturn;

      const out: SseEvent[] = [];
      for (const line of lines) {
        processLine(line, out);
      }
      return out;
    },

    flush(): SseEvent[] {
      const out: SseEvent[] = [];
      buffer = normalize(buffer);
      const lines = buffer.split("\n");
      buffer = "";
      for (const line of lines) {
        if (line !== "") {
          processLine(line, out);
        }
      }
      dispatch(out);
      return out;
    },
  };
}
