/**
 * État d'un Tour texte, réduit depuis le flux SSE.
 *
 * Module **pur** : aucun DOM, aucun `fetch`. Le flux ne dit que des morceaux de
 * texte, de l'audio, des appels de Tools et une fin ; cet état rassemble ce qui
 * s'affiche pendant la réponse et ce qui reste après elle (ADR-0024).
 */

import type { SseEvent } from "../../api/sse";
import { decodeBase64 } from "./audio";

/** Statut d'un appel de Tool tel que le flux le rapporte. */
export type ToolStatus = "ok" | "error";

/** Appel de Tool visible pendant le Tour. */
export interface ToolCallView {
  name: string;
  /** Arguments déjà mis en forme pour l'affichage, jamais un Secret. */
  arguments: string;
  status: ToolStatus;
}

/** Phase d'un Tour : en cours, puis l'une des fins possibles. */
export type TurnPhase = "streaming" | "done" | "busy" | "error" | "canceled";

/** Ce que l'écran affiche d'un Tour, du message envoyé à sa fin. */
export interface TurnState {
  message: string;
  reply: string;
  tools: readonly ToolCallView[];
  phase: TurnPhase;
  /** Raison du `done` ; `null` tant que le Tour n'est pas fini. */
  reason: string | null;
  /** Code stable d'une panne ou du refus « occupée ». */
  errorCode: string | null;
  /** Vrai dès qu'un événement terminal a clos le flux. */
  finished: boolean;
}

/** Événement décodé du flux, prêt à réduire. */
export type TourEvent =
  | { type: "text"; text: string }
  | { type: "audio"; pcm: Uint8Array }
  | {
      type: "tool";
      name: string;
      arguments: unknown;
      status: ToolStatus;
    }
  | { type: "done"; reason: string }
  | { type: "error"; code: string }
  | { type: "busy" }
  | { type: "unknown" };

/** Un Tour commence dès que le message est accepté par l'écran. */
export function startTurn(message: string): TurnState {
  return {
    message,
    reply: "",
    tools: [],
    phase: "streaming",
    reason: null,
    errorCode: null,
    finished: false,
  };
}

/** Vrai pour un événement qui referme le flux : au-delà, rien n'est ajouté. */
export function isTerminalEvent(event: TourEvent): boolean {
  return event.type === "done" || event.type === "error" || event.type === "busy";
}

/** Réduit un événement dans l'état du Tour ; un Tour fini ne change plus. */
export function applyTourEvent(state: TurnState, event: TourEvent): TurnState {
  if (state.finished) {
    return state;
  }
  switch (event.type) {
    case "text":
      return { ...state, reply: state.reply + event.text };
    case "tool":
      return {
        ...state,
        tools: [
          ...state.tools,
          {
            name: event.name,
            arguments: formatArguments(event.arguments),
            status: event.status,
          },
        ],
      };
    case "done":
      return { ...state, phase: "done", reason: event.reason, finished: true };
    case "error":
      return {
        ...state,
        phase: "error",
        errorCode: event.code,
        finished: true,
      };
    case "busy":
      return { ...state, phase: "busy", errorCode: "busy", finished: true };
    case "audio":
    case "unknown":
      return state;
  }
}

/** Le flux s'est refermé sans événement terminal : le Tour est clos. */
export function finishTurn(state: TurnState): TurnState {
  if (state.finished) {
    return state;
  }
  return { ...state, phase: "done", reason: "closed", finished: true };
}

/** L'utilisateur a interrompu : le texte déjà reçu reste lisible. */
export function cancelTurn(state: TurnState): TurnState {
  if (state.finished) {
    return state;
  }
  return {
    ...state,
    phase: "canceled",
    reason: "canceled",
    finished: true,
  };
}

/** Libellé français de la raison d'un `done`. */
export function doneReasonLabel(reason: string): string {
  switch (reason) {
    case "completed":
      return "Terminé";
    case "canceled":
      return "Interrompu";
    case "timeout":
      return "Délai dépassé";
    case "closed":
      return "Connexion fermée";
    default:
      return "Terminé";
  }
}

/** Décode une trame SSE du Tour, sans jamais lever sur un contenu inattendu. */
export function decodeTourFrame(frame: SseEvent): TourEvent {
  if (frame.event === "busy") {
    return { type: "busy" };
  }
  const payload = parsePayload(frame.data);
  if (payload === null) {
    return { type: "unknown" };
  }
  switch (frame.event) {
    case "text":
      return typeof payload.text === "string"
        ? { type: "text", text: payload.text }
        : { type: "unknown" };
    case "audio":
      // Seul le PCM annoncé par le contrat est joué : un format inconnu reste
      // ignoré plutôt que d'être interprété comme du son.
      if (
        typeof payload.data !== "string" ||
        !isPcmMimeType(payload.mime_type)
      ) {
        return { type: "unknown" };
      }
      return { type: "audio", pcm: decodeBase64(payload.data) };
    case "tool":
      if (typeof payload.name !== "string" || payload.name === "") {
        return { type: "unknown" };
      }
      return {
        type: "tool",
        name: payload.name,
        arguments: payload.arguments,
        status: payload.status === "error" ? "error" : "ok",
      };
    case "done":
      return {
        type: "done",
        reason:
          typeof payload.reason === "string" && payload.reason !== ""
            ? payload.reason
            : "closed",
      };
    case "error":
      return {
        type: "error",
        code:
          typeof payload.error === "string" && payload.error !== ""
            ? payload.error
            : "tour_failed",
      };
    default:
      return { type: "unknown" };
  }
}

/** Le contrat du Tour : PCM 16 bits mono 24 kHz, jamais un autre format. */
function isPcmMimeType(value: unknown): boolean {
  if (value === undefined) {
    return true;
  }
  return typeof value === "string" && value.startsWith("audio/pcm");
}

function parsePayload(data: string): Record<string, unknown> | null {
  if (data === "") {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return null;
  }
  return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : null;
}

function formatArguments(value: unknown): string {
  if (value === undefined || value === null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return "";
  }
}
