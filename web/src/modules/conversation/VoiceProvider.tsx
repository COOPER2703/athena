import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { describeCode } from "../../api/errors";
import { connectVoice, type VoiceConnection } from "../../api/voice";
import {
  CaptureError,
  prepareCaptureContext,
  releaseCaptureContext,
  startCapture,
  voiceSupport,
  type VoiceCapture,
} from "./capture";
import { useAudioPlayer } from "./useAudioPlayer";
import {
  applyVoiceEvent,
  bargeInDetected,
  idleVoice,
  voiceBarVisible,
  type VoiceEvent,
  type VoiceState,
} from "./voice";

/** Ce que la coquille et la Conversation consomment de la voix. */
export interface VoiceConversation {
  state: VoiceState;
  /**
   * Un tap démarre la conversation (jamais de push-to-talk ni de Wake word).
   * `baselineId` est l'identifiant maximal du Journal déjà lu : le fil s'en
   * sert pour ne pas confondre une parole ancienne avec un mot en direct.
   */
  start(baselineId: number): void;
  /** La croix termine et rend la barre de saisie. */
  stop(): void;
  /** « Reprendre » après une coupure, avec le micro déjà ouvert. */
  resume(): void;
  /** Ferme le bandeau d'une conversation terminée ou refusée. */
  dismiss(): void;
  /** Oublie les `drop` premières lignes, reprises par la relecture du fil. */
  settle(drop: number): void;
  /** Niveau RMS du micro pour l'onde, ou `null` si l'analyse est indisponible. */
  getLevel(): number | null;
}

const VoiceContext = createContext<VoiceConversation | null>(null);

/** Délai court avant la tentative automatique : le serveur a déjà refermé. */
const RECONNECT_DELAY_MS = 400;

/**
 * La conversation vocale vit **au-dessus du routeur**.
 *
 * Changer de zone ne démonte ni le socket, ni le micro, ni le lecteur audio :
 * seule cette coquille les possède. Elle ne démarre jamais toute seule et ne
 * demande le micro qu'après la trame `ready` ; une reprise réutilise le flux
 * déjà obtenu, sans nouvelle permission (ADR-0027).
 *
 * `active` suit le Ticket Console : hors application, le fournisseur reste
 * monté (le routeur ne se remonte pas) mais libère micro, socket et lecture.
 */
export function VoiceProvider({
  active,
  children,
}: {
  active: boolean;
  children: ReactNode;
}) {
  const [state, setState] = useState<VoiceState>(idleVoice);
  const stateRef = useRef<VoiceState>(state);
  const socketRef = useRef<VoiceConnection | null>(null);
  const socketReadyRef = useRef(false);
  const captureRef = useRef<VoiceCapture | null>(null);
  const preparedContextRef = useRef<AudioContext | null>(null);
  const generationRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bargeSamplesRef = useRef(0);
  const bargeStoppedRef = useRef(false);
  const openSocketRef = useRef<(mode: "start" | "reconnect") => void>(
    () => undefined,
  );
  const player = useAudioPlayer();

  const apply = useCallback((event: VoiceEvent): VoiceState => {
    const next = applyVoiceEvent(stateRef.current, event);
    if (next !== stateRef.current) {
      stateRef.current = next;
      setState(next);
    }
    return next;
  }, []);

  const clearReconnect = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const stopCapture = useCallback(() => {
    const capture = captureRef.current;
    captureRef.current = null;
    capture?.stop();
  }, []);

  const releasePrepared = useCallback(() => {
    const context = preparedContextRef.current;
    preparedContextRef.current = null;
    releaseCaptureContext(context);
  }, []);

  const closeSocket = useCallback(() => {
    // Toute trame tardive du socket précédent est ignorée : une reprise crée
    // un nouveau numéro de génération.
    generationRef.current += 1;
    socketReadyRef.current = false;
    const socket = socketRef.current;
    socketRef.current = null;
    socket?.close();
  }, []);

  /** Fin annoncée, croix ou panne : plus de socket, plus de micro, plus d'audio. */
  const finish = useCallback(
    (event: VoiceEvent) => {
      clearReconnect();
      closeSocket();
      player.stop();
      stopCapture();
      releasePrepared();
      apply(event);
    },
    [apply, clearReconnect, closeSocket, player, releasePrepared, stopCapture],
  );

  const beginCapture = useCallback(
    (generation: number) => {
      const context = preparedContextRef.current;
      void startCapture({
        context,
        onFrame: (pcm) => {
          if (generation !== generationRef.current || !socketReadyRef.current) {
            return;
          }
          socketRef.current?.send(pcm);
        },
      })
        .then((capture) => {
          if (preparedContextRef.current === context) {
            preparedContextRef.current = null;
          }
          if (generation !== generationRef.current) {
            capture.stop();
            return;
          }
          captureRef.current = capture;
          // « Parlez » ne s'affiche qu'une fois le micro réellement ouvert.
          apply({ type: "ready" });
        })
        .catch((reason: unknown) => {
          if (preparedContextRef.current === context) {
            preparedContextRef.current = null;
          }
          if (generation !== generationRef.current) {
            return;
          }
          const code =
            reason instanceof CaptureError
              ? reason.code
              : "audio_worklet_failed";
          finish({ type: "failed", code, message: describeCode(code) });
        });
    },
    [apply, finish],
  );

  const openSocket = useCallback(
    (mode: "start" | "reconnect") => {
      clearReconnect();
      closeSocket();
      const generation = generationRef.current;

      const connection = connectVoice({
        onReady: () => {
          if (generation !== generationRef.current) {
            return;
          }
          const phase = stateRef.current.phase;
          if (phase !== "connecting" && phase !== "reconnecting") {
            return;
          }
          // À partir d'ici seulement, les trames du micro peuvent partir.
          socketReadyRef.current = true;
          if (captureRef.current !== null) {
            // Reprise : le flux existant repart tel quel, sans getUserMedia.
            apply({ type: "ready" });
            return;
          }
          if (mode === "reconnect") {
            finish({
              type: "failed",
              code: "microphone_lost",
              message: describeCode("microphone_lost"),
            });
            return;
          }
          beginCapture(generation);
        },
        onTranscript: (role, text) => {
          if (generation !== generationRef.current) {
            return;
          }
          if (role === "user") {
            // Barge-in local : le serveur gère l'interruption Gemini, la page
            // coupe seulement ce qui était déjà en file.
            player.stop();
            bargeSamplesRef.current = 0;
            bargeStoppedRef.current = true;
          }
          apply({ type: "transcript", role, text });
        },
        onAudio: (pcm) => {
          if (generation !== generationRef.current) {
            return;
          }
          bargeStoppedRef.current = false;
          player.push(pcm);
          apply({ type: "audio" });
        },
        onEnded: (reason) => {
          if (generation !== generationRef.current) {
            return;
          }
          finish({ type: "ended", reason });
        },
        onRefused: (code, message) => {
          if (generation !== generationRef.current) {
            return;
          }
          finish({ type: "refused", code, message });
        },
        onClose: () => {
          if (generation !== generationRef.current) {
            return;
          }
          player.stop();
          socketReadyRef.current = false;
          const next = apply({ type: "cut" });
          // Un éventuel getUserMedia encore en vol ne doit pas ouvrir le micro
          // pour un socket mort ; le contexte préparé n'a plus de preneur.
          generationRef.current += 1;
          releasePrepared();
          if (next.phase === "refused") {
            // Aucune conversation à reprendre : la capture éventuelle (reprise
            // manuelle) est libérée, la barre de saisie revient.
            clearReconnect();
            stopCapture();
            return;
          }
          if (next.phase !== "reconnecting") {
            return;
          }
          reconnectTimerRef.current = setTimeout(() => {
            reconnectTimerRef.current = null;
            if (stateRef.current.phase === "reconnecting") {
              openSocketRef.current("reconnect");
            }
          }, RECONNECT_DELAY_MS);
        },
      });

      socketRef.current = connection;
    },
    [
      apply,
      beginCapture,
      clearReconnect,
      closeSocket,
      finish,
      player,
      releasePrepared,
      stopCapture,
    ],
  );
  useEffect(() => {
    openSocketRef.current = openSocket;
  }, [openSocket]);

  const start = useCallback((baselineId: number) => {
    if (!active || voiceBarVisible(stateRef.current.phase)) {
      return;
    }
    // Un démarrage neuf repart d'un micro propre : une ancienne capture en
    // attente ou une interruption laissée ouverte ne doit pas survivre.
    clearReconnect();
    closeSocket();
    stopCapture();
    releasePrepared();
    const support = voiceSupport();
    if (!support.ok) {
      // Une tentative qui échoue avant le socket laisse la barre de saisie et
      // un message : « Parler » n'est jamais inerte.
      apply({ type: "start", baselineId });
      finish({
        type: "refused",
        code: support.code,
        message: describeCode(support.code),
      });
      return;
    }
    // Le tap est le geste qui débloque la lecture **et** le contexte de
    // capture : celui-ci est créé maintenant, le micro n'ouvrira qu'après
    // `ready`.
    preparedContextRef.current = prepareCaptureContext();
    player.unlock();
    apply({ type: "start", baselineId });
    openSocket("start");
  }, [
    active,
    apply,
    clearReconnect,
    closeSocket,
    finish,
    openSocket,
    player,
    releasePrepared,
    stopCapture,
  ]);

  const resume = useCallback(() => {
    if (!active || stateRef.current.phase !== "interrupted") {
      return;
    }
    if (captureRef.current === null) {
      // On ne redemande jamais la permission en reprise : sans flux vivant, la
      // conversation se termine et un nouveau départ repassera par le micro.
      finish({
        type: "failed",
        code: "microphone_lost",
        message: describeCode("microphone_lost"),
      });
      return;
    }
    player.unlock();
    apply({ type: "resume" });
    openSocket("reconnect");
  }, [active, apply, finish, openSocket, player]);

  const stop = useCallback(() => {
    if (!voiceBarVisible(stateRef.current.phase)) {
      return;
    }
    finish({ type: "stop" });
  }, [finish]);

  const dismiss = useCallback(() => {
    const phase = stateRef.current.phase;
    if (phase === "interrupted") {
      finish({ type: "dismiss" });
      return;
    }
    apply({ type: "dismiss" });
  }, [apply, finish]);

  const settle = useCallback(
    (drop: number) => {
      apply({ type: "settle", drop });
    },
    [apply],
  );

  const getLevel = useCallback((): number | null => {
    return captureRef.current?.readLevel() ?? null;
  }, []);

  // Barge-in local : deux mesures hautes consécutives coupent la file de
  // lecture. Le serveur, lui, reçoit l'audio et laisse Gemini interrompre.
  useEffect(() => {
    if (state.phase !== "replying") {
      bargeSamplesRef.current = 0;
      return;
    }
    const timer = setInterval(() => {
      const level = captureRef.current?.readLevel();
      if (level === null || level === undefined) {
        return;
      }
      if (bargeInDetected(stateRef.current, level)) {
        bargeSamplesRef.current += 1;
        if (bargeSamplesRef.current >= 2 && !bargeStoppedRef.current) {
          player.stop();
          bargeStoppedRef.current = true;
        }
      } else {
        bargeSamplesRef.current = 0;
      }
    }, 80);
    return () => clearInterval(timer);
  }, [player, state.phase]);

  // Mobile : le système suspend le micro en arrière-plan. La conversation se
  // termine avec un message, au lieu de rester ouverte sans capter. Sur
  // bureau, l'onglet masqué ne change rien.
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState !== "hidden" || !isMobileLike()) {
        return;
      }
      if (!voiceBarVisible(stateRef.current.phase)) {
        return;
      }
      finish({ type: "background" });
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [finish]);

  // Démonter la coquille (déconnexion, changement de compte) libère tout.
  useEffect(
    () => () => {
      clearReconnect();
      closeSocket();
      stopCapture();
      releasePrepared();
    },
    [clearReconnect, closeSocket, releasePrepared, stopCapture],
  );

  // Le Ticket n'est plus valide (déconnexion, écran de connexion) : la
  // conversation ne survit pas à l'identité. Le fournisseur reste monté pour
  // ne pas remonter le routeur, mais tout est libéré et l'état revient au
  // repos.
  useEffect(() => {
    if (active) {
      return;
    }
    clearReconnect();
    closeSocket();
    player.stop();
    stopCapture();
    releasePrepared();
    bargeSamplesRef.current = 0;
    bargeStoppedRef.current = false;
    const reset = idleVoice();
    stateRef.current = reset;
    setState(reset);
  }, [
    active,
    clearReconnect,
    closeSocket,
    player,
    releasePrepared,
    stopCapture,
  ]);

  const value = useMemo<VoiceConversation>(
    () => ({ state, start, stop, resume, dismiss, settle, getLevel }),
    [dismiss, getLevel, resume, settle, start, state, stop],
  );

  return (
    <VoiceContext.Provider value={value}>{children}</VoiceContext.Provider>
  );
}

/** Accès à la conversation vocale ; exige le fournisseur au-dessus du routeur. */
export function useVoiceConversation(): VoiceConversation {
  const value = useContext(VoiceContext);
  if (value === null) {
    throw new Error(
      "useVoiceConversation doit être utilisé sous VoiceProvider.",
    );
  }
  return value;
}

/** Mobile ou tablette : pointeur grossier, donc micro premier plan seulement. */
function isMobileLike(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia("(pointer: coarse)").matches;
}
