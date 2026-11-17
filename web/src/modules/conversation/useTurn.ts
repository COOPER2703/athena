import { useCallback, useEffect, useRef, useState } from "react";

import { openTour } from "../../api/chat";
import { ApiError, isApiError } from "../../api/errors";
import { createSseParser, type SseEvent } from "../../api/sse";
import {
  applyTourEvent,
  cancelTurn,
  decodeTourFrame,
  finishTurn,
  isTerminalEvent,
  startTurn,
  type TurnState,
} from "./turn";

/** Refus ou panne d'un Tour, conservé au-dessus de la barre de saisie. */
export interface TurnIssue {
  kind: "busy" | "error";
  /** Code stable, traduit par `describeCode` à l'affichage. */
  code: string;
  /** Message envoyé : il reste proposé pour un nouvel essai. */
  message: string;
}

export interface UseTurnOptions {
  /** Relit le fil après un Tour terminé ; `true` si la relecture a réussi. */
  onFinished: () => Promise<boolean>;
  /** Morceau d'audio reçu ; l'écran décide de le jouer ou non. */
  onAudio: (pcm: Uint8Array) => void;
}

export interface TurnController {
  /** Tour en cours ou venant de finir, tant que le fil ne l'a pas repris. */
  turn: TurnState | null;
  /** Refus « occupée » ou panne à expliquer, avec réessai. */
  issue: TurnIssue | null;
  streaming: boolean;
  send: (text: string) => void;
  cancel: () => void;
  retry: () => void;
  dismissIssue: () => void;
}

/**
 * Pilote un Tour texte : envoi, lecture du flux SSE, annulation et réessai.
 *
 * Fermer la requête (`cancel`) annule réellement le Tour côté serveur. Une fin
 * terminale relit le fil, puis efface le Tour local dès que le Journal le
 * reprend : le message n'est jamais affiché deux fois.
 */
export function useTurn({
  onFinished,
  onAudio,
}: UseTurnOptions): TurnController {
  const [turn, setTurn] = useState<TurnState | null>(null);
  const [issue, setIssue] = useState<TurnIssue | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const runningRef = useRef(false);
  const mountedRef = useRef(true);
  const lastMessageRef = useRef("");
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;
  const audioRef = useRef(onAudio);
  audioRef.current = onAudio;

  // Quitter la zone referme la réponse : le Tour est annulé côté serveur au
  // lieu d'occuper seul le noyau pendant une navigation.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const send = useCallback((raw: string) => {
    const text = raw.trim();
    if (text === "" || runningRef.current) {
      return;
    }
    runningRef.current = true;
    lastMessageRef.current = text;
    setIssue(null);

    const controller = new AbortController();
    abortRef.current = controller;
    let state = startTurn(text);
    setTurn(state);

    const absorb = (frames: readonly SseEvent[]): boolean => {
      for (const frame of frames) {
        const event = decodeTourFrame(frame);
        if (event.type === "audio") {
          audioRef.current(event.pcm);
          continue;
        }
        if (event.type === "unknown") {
          continue;
        }
        state = applyTourEvent(state, event);
        if (mountedRef.current) {
          setTurn(state);
        }
        if (isTerminalEvent(event)) {
          return true;
        }
      }
      return false;
    };

    /** Efface le Tour local si la relecture du fil l'a bien repris. */
    const settle = async (final: TurnState): Promise<void> => {
      runningRef.current = false;
      if (!mountedRef.current) {
        return;
      }
      if (final.phase === "busy") {
        setIssue({ kind: "busy", code: "busy", message: text });
        setTurn((current) => (current === final ? null : current));
        return;
      }
      if (final.phase === "canceled") {
        // Le Journal n'a pas la réponse partielle : on la garde à l'écran
        // jusqu'au Tour suivant, plutôt que de l'effacer sous les yeux.
        return;
      }
      if (final.phase === "error" && final.errorCode !== null) {
        setIssue({ kind: "error", code: final.errorCode, message: text });
      }
      const refreshed = await finishedRef.current().catch(() => false);
      if (refreshed) {
        setTurn((current) => (current === final ? null : current));
      }
    };

    void (async () => {
      try {
        const response = await openTour(text, controller.signal);
        const body = response.body;
        if (body === null) {
          throw new ApiError(0, "invalid_response");
        }
        const reader = body.getReader();
        const decoder = new TextDecoder();
        const parser = createSseParser();
        let terminal = false;
        try {
          while (!terminal) {
            const { done, value } = await reader.read();
            if (done) {
              terminal =
                absorb(parser.push(decoder.decode())) ||
                absorb(parser.flush());
              break;
            }
            terminal = absorb(
              parser.push(decoder.decode(value, { stream: true })),
            );
          }
        } finally {
          reader.releaseLock();
        }
        if (!terminal && !state.finished) {
          state = finishTurn(state);
          if (mountedRef.current) {
            setTurn(state);
          }
        }
        await settle(state);
      } catch (reason) {
        if (controller.signal.aborted) {
          state = cancelTurn(state);
          if (mountedRef.current) {
            setTurn(state);
          }
          await settle(state);
          return;
        }
        runningRef.current = false;
        if (state.finished || !mountedRef.current) {
          // Une fin terminale déjà reçue fait foi : un incident de relecture
          // ne la transforme pas en panne.
          return;
        }
        const code = isApiError(reason) ? reason.code : "network";
        state = applyTourEvent(state, { type: "error", code });
        setTurn(state);
        setIssue({ kind: "error", code, message: text });
      }
    })();
  }, []);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const retry = useCallback(() => {
    if (lastMessageRef.current !== "") {
      send(lastMessageRef.current);
    }
  }, [send]);

  const dismissIssue = useCallback(() => {
    setIssue(null);
  }, []);

  return {
    turn,
    issue,
    streaming: turn !== null && turn.phase === "streaming",
    send,
    cancel,
    retry,
    dismissIssue,
  };
}
