/**
 * Conversation vocale de la Console : état, trames et phrases visibles.
 *
 * Module **pur** : aucun DOM, aucun `fetch`, aucun `WebSocket`. La machine à
 * états ne connaît que des événements déjà décodés ; le transport et la capture
 * audio vivent à côté. C'est ce qui rend testables les règles qui comptent :
 * quand le micro s'ouvre, combien de reconnexions automatiques sont permises,
 * et quel motif ramène la barre de saisie (ADR-0027).
 */

import { decodeBase64 } from "./audio";

/** Qui a prononcé une transcription. */
export type VoiceRole = "user" | "assistant";

/** Phase visible de la conversation. */
export type VoicePhase =
  | "idle"
  | "connecting"
  | "listening"
  | "replying"
  | "reconnecting"
  | "interrupted"
  | "ended"
  | "refused";

/** Motif de fin, normalisé depuis le protocole (majuscules serveur). */
export type VoiceEndReason =
  | "user"
  | "background"
  | "completed"
  | "timeout"
  | "client_gone"
  | "normal"
  | "error"
  | "shutdown"
  | "replaced";

/** Une transcription, gardée dans l'ordre où elle est arrivée. */
export interface VoiceLine {
  role: VoiceRole;
  text: string;
}

/** Refus ou panne, avec un code stable et une phrase déjà lisible. */
export interface VoiceProblem {
  code: string;
  message: string;
}

/** Tout ce que l'écran a besoin de savoir d'une conversation. */
export interface VoiceState {
  phase: VoicePhase;
  /** Mots de l'utilisateur et réponses d'Athena, dans l'ordre. */
  lines: readonly VoiceLine[];
  /** Vrai dès qu'une reconnexion automatique a été consommée. */
  automaticReconnectUsed: boolean;
  /** Motif de fin, quand `phase` vaut `ended`. */
  endReason: VoiceEndReason | null;
  /** Refus, panne ou échec de reprise, jamais un message brut du noyau. */
  problem: VoiceProblem | null;
}

/** Trame descendante décodée du contrat `GET /api/voice`. */
export type VoiceFrame =
  | { type: "ready" }
  | { type: "transcript"; role: VoiceRole; text: string }
  | { type: "audio"; pcm: Uint8Array }
  | { type: "ended"; reason: VoiceEndReason }
  | { type: "refused"; code: string; message: string };

/**
 * Événement réduit par la machine : trames et décisions locales.
 *
 * L'audio de la machine ne porte pas ses octets : le fournisseur les joue, la
 * machine retient seulement qu'Athena a commencé à répondre.
 */
export type VoiceEvent =
  | { type: "ready" }
  | { type: "transcript"; role: VoiceRole; text: string }
  | { type: "audio" }
  | { type: "ended"; reason: string }
  | { type: "refused"; code: string; message: string }
  | { type: "start" }
  | { type: "resume" }
  | { type: "cut" }
  | { type: "failed"; code: string; message: string }
  | { type: "stop" }
  | { type: "background" }
  | { type: "dismiss" };

/** États où la barre vocale remplace la barre de saisie. */
const BAR_PHASES: ReadonlySet<VoicePhase> = new Set([
  "connecting",
  "listening",
  "replying",
  "reconnecting",
  "interrupted",
]);

/** États où une trame du serveur peut encore faire avancer la machine. */
const ACTIVE_PHASES: ReadonlySet<VoicePhase> = new Set([
  "connecting",
  "listening",
  "replying",
  "reconnecting",
]);

/** Message exact de la coupure pendant la tentative automatique. */
export const VOICE_RECONNECTING_NOTICE =
  "Conversation interrompue — tentative de reconnexion en cours";

/** Message de l'état « Interrompue », quand la tentative unique a échoué. */
export const VOICE_INTERRUPTED_NOTICE =
  "Conversation interrompue — la liaison n'a pas pu être rétablie. « Reprendre » relance la conversation avec le micro déjà ouvert.";

/** Message générique d'une coupure, sans promesse de reprise. */
export const VOICE_CUT_MESSAGE =
  "La liaison avec Athena a été interrompue.";

/** Message d'une liaison qui n'a jamais abouti, ou d'une reprise manquée. */
export const VOICE_CONNECT_FAILED_MESSAGE =
  "La conversation vocale n'a pas pu joindre Athena. Vérifiez la connexion, puis réessayez.";

/**
 * Seuil RMS au-dessus duquel la voix de l'utilisateur coupe la lecture locale.
 *
 * Le serveur gère le barge-in réel ; ce seuil ne fait qu'arrêter le son déjà
 * en file pour ne pas laisser Athena parler par-dessus la personne.
 */
export const BARGE_IN_LEVEL = 0.12;

/** Conversation au repos : aucune session, aucune ligne, aucun motif. */
export function idleVoice(): VoiceState {
  return {
    phase: "idle",
    lines: [],
    automaticReconnectUsed: false,
    endReason: null,
    problem: null,
  };
}

/** Vrai quand la barre vocale doit remplacer la barre de saisie. */
export function voiceBarVisible(phase: VoicePhase): boolean {
  return BAR_PHASES.has(phase);
}

/** Libellé français de l'état visible. */
export function voicePhaseLabel(phase: VoicePhase): string {
  switch (phase) {
    case "idle":
      return "";
    case "connecting":
      return "Connexion…";
    case "listening":
      return "Parlez";
    case "replying":
      return "Athena répond";
    case "reconnecting":
      return "Reconnexion…";
    case "interrupted":
      return "Interrompue";
    case "ended":
      return "Terminée";
    case "refused":
      return "Refusée";
  }
}

/** Libellé d'état d'une conversation close, tel que l'écran le montre. */
export function voiceEndStatus(
  reason: VoiceEndReason,
): "Terminée" | "Interrompue" {
  switch (reason) {
    case "client_gone":
    case "error":
    case "shutdown":
    case "replaced":
      return "Interrompue";
    default:
      return "Terminée";
  }
}

/** Titre du bandeau affiché après la fin d'une conversation. */
export function voiceEndTitle(reason: VoiceEndReason): string {
  switch (reason) {
    case "client_gone":
    case "error":
    case "shutdown":
    case "replaced":
      return "Conversation interrompue";
    default:
      return "Conversation terminée";
  }
}

/**
 * Phrase de fin, une par cause.
 *
 * La croix, une fin décidée par Athena, le timeout, une coupure et un
 * changement d'appareil ne se racontent pas de la même façon : la personne doit
 * savoir si elle a agi, si Athena a conclu, ou si le système a coupé.
 */
export function voiceEndMessage(reason: VoiceEndReason): string {
  switch (reason) {
    case "user":
      return "Vous avez terminé la conversation. La barre de saisie est de nouveau disponible.";
    case "background":
      return "Conversation terminée : sur mobile, le système suspend le micro dès que la page passe en arrière-plan.";
    case "completed":
      return "Athena a terminé la conversation.";
    case "timeout":
      return "Conversation terminée après une longue inactivité.";
    case "client_gone":
      return "La liaison vocale s'est fermée.";
    case "normal":
      return "La conversation est terminée.";
    case "error":
      return "La conversation s'est arrêtée sur une erreur.";
    case "shutdown":
      return "Le serveur s'est arrêté ; la conversation est terminée.";
    case "replaced":
      return "Un autre appareil a repris la conversation.";
  }
}

/** Vrai quand la voix de l'utilisateur doit couper la lecture locale. */
export function bargeInDetected(state: VoiceState, level: number): boolean {
  return state.phase === "replying" && level >= BARGE_IN_LEVEL;
}

/** Réduit un événement dans l'état de conversation, sans jamais lever. */
export function applyVoiceEvent(
  state: VoiceState,
  event: VoiceEvent,
): VoiceState {
  switch (event.type) {
    case "start":
      // Un second tap pendant la conversation ne relance rien ; un refus, une
      // fin ou une interruption se relancent depuis le début.
      if (voiceBarVisible(state.phase)) {
        return state;
      }
      return beginConversation();

    case "resume":
      if (state.phase !== "interrupted") {
        return state;
      }
      // La reprise manuelle réarme la tentative automatique : c'est un nouveau
      // départ décidé par la personne, pas un second essai silencieux.
      return {
        ...state,
        phase: "connecting",
        automaticReconnectUsed: false,
        endReason: null,
        problem: null,
      };

    case "ready":
      if (state.phase !== "connecting" && state.phase !== "reconnecting") {
        return state;
      }
      // « Parlez » ne s'affiche qu'après le feu vert du serveur : le micro ne
      // capte pas avant que la Connexion Live soit ouverte.
      return { ...state, phase: "listening" };

    case "transcript":
      if (!ACTIVE_PHASES.has(state.phase) || event.text === "") {
        return state;
      }
      return {
        ...state,
        lines: [...state.lines, { role: event.role, text: event.text }],
        phase: event.role === "user" ? "listening" : "replying",
      };

    case "audio":
      if (state.phase === "replying") {
        // Les morceaux suivants ne changent plus l'état : seul le premier
        // ouvre « Athena répond ».
        return state;
      }
      if (
        state.phase !== "connecting" &&
        state.phase !== "listening"
      ) {
        return state;
      }
      return { ...state, phase: "replying" };

    case "ended":
      if (!ACTIVE_PHASES.has(state.phase)) {
        return state;
      }
      return {
        ...state,
        phase: "ended",
        endReason: normalizeEndReason(event.reason),
        problem: null,
      };

    case "refused":
      if (!ACTIVE_PHASES.has(state.phase)) {
        return state;
      }
      return {
        ...state,
        phase: "refused",
        endReason: null,
        problem: { code: event.code, message: event.message },
      };

    case "cut":
      return cut(state);

    case "failed":
      return fail(state, { code: event.code, message: event.message });

    case "stop":
      if (!voiceBarVisible(state.phase)) {
        return state;
      }
      return {
        ...state,
        phase: "ended",
        endReason: "user",
        problem: null,
      };

    case "background":
      if (!voiceBarVisible(state.phase)) {
        return state;
      }
      return {
        ...state,
        phase: "ended",
        endReason: "background",
        problem: null,
      };

    case "dismiss":
      if (
        state.phase === "ended" ||
        state.phase === "refused" ||
        state.phase === "interrupted"
      ) {
        return idleVoice();
      }
      return state;
  }
}

/** Décode une trame du WebSocket vocal, sans jamais lever. */
export function decodeVoiceFrame(payload: unknown): VoiceFrame | null {
  if (!isRecord(payload) || payload.v !== 1) {
    return null;
  }
  const data = payload.data;
  if (!isRecord(data)) {
    return null;
  }
  switch (payload.type) {
    case "ready":
      return { type: "ready" };
    case "transcript": {
      const role = data.role;
      const text = data.text;
      if (
        (role !== "user" && role !== "assistant") ||
        typeof text !== "string" ||
        text === ""
      ) {
        return null;
      }
      return { type: "transcript", role, text };
    }
    case "audio": {
      const mime = data.mime;
      const encoded = data.data;
      if (
        typeof mime !== "string" ||
        !mime.startsWith("audio/pcm") ||
        typeof encoded !== "string"
      ) {
        return null;
      }
      return { type: "audio", pcm: decodeBase64(encoded) };
    }
    case "ended": {
      const reason = data.reason;
      return {
        type: "ended",
        reason: normalizeEndReason(typeof reason === "string" ? reason : ""),
      };
    }
    case "refused": {
      const code = data.code;
      const message = data.message;
      if (typeof code !== "string" || code === "") {
        return null;
      }
      return {
        type: "refused",
        code,
        message: typeof message === "string" ? message : "",
      };
    }
    default:
      return null;
  }
}

function beginConversation(): VoiceState {
  return {
    phase: "connecting",
    lines: [],
    automaticReconnectUsed: false,
    endReason: null,
    problem: null,
  };
}

/**
 * Coupure de la liaison.
 *
 * Seule une conversation déjà en écoute ou en réponse consomme l'unique
 * reconnexion automatique. Une première liaison qui n'aboutit jamais, ou une
 * tentative de reprise qui échoue, va directement à « Interrompue » : sinon le
 * bouton « Reprendre » n'aurait plus rien à proposer.
 */
function cut(state: VoiceState): VoiceState {
  if (state.phase === "listening" || state.phase === "replying") {
    if (!state.automaticReconnectUsed) {
      return {
        ...state,
        phase: "reconnecting",
        automaticReconnectUsed: true,
        problem: null,
      };
    }
    return {
      ...state,
      phase: "interrupted",
      problem: { code: "voice_cut", message: VOICE_CUT_MESSAGE },
    };
  }
  if (state.phase === "connecting") {
    // La liaison n'a jamais abouti : rien à reprendre, le micro n'a pas encore
    // été ouvert. Un nouveau tap repassera proprement par la permission.
    return {
      ...state,
      phase: "refused",
      problem: {
        code: "voice_connect_failed",
        message: VOICE_CONNECT_FAILED_MESSAGE,
      },
    };
  }
  if (state.phase === "reconnecting") {
    // La tentative automatique a échoué : le micro est toujours ouvert, donc
    // « Reprendre » peut réessayer sans redemander la permission.
    return {
      ...state,
      phase: "interrupted",
      problem: { code: "voice_cut", message: VOICE_CUT_MESSAGE },
    };
  }
  return state;
}

/** Panne locale : refus au démarrage, reprise impossible, ou fin en erreur. */
function fail(state: VoiceState, problem: VoiceProblem): VoiceState {
  if (state.phase === "connecting" || state.phase === "interrupted") {
    return { ...state, phase: "refused", problem };
  }
  if (state.phase === "reconnecting") {
    return { ...state, phase: "interrupted", problem };
  }
  if (state.phase === "listening" || state.phase === "replying") {
    return { ...state, phase: "ended", endReason: "error", problem };
  }
  return state;
}

function normalizeEndReason(reason: string): VoiceEndReason {
  switch (reason.toLowerCase()) {
    case "completed":
      return "completed";
    case "timeout":
      return "timeout";
    case "client_gone":
      return "client_gone";
    case "error":
      return "error";
    case "shutdown":
      return "shutdown";
    case "replaced":
      return "replaced";
    case "normal":
    default:
      return "normal";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
