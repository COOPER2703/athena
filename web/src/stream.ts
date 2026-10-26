/**
 * Réduction des trames du WebSocket en état de page.
 *
 * Une seule fonction pure applique `hello`, `status` et `trace` : la
 * resynchronisation après redémarrage du serveur se teste donc sans navigateur.
 * Un `hello` remplace l'état et le log ; une trame `status` ne touche pas au
 * flux ; une trame `trace` ne touche pas à l'état.
 */

import { normalizeStatus, statusFromHello, type StatusData } from "./status";
import {
  applyTrace,
  emptyFeed,
  resetOnHello,
  type FeedState,
  type StreamFrame,
} from "./trace";

/** Tout ce que la page affiche et qui vient du WebSocket. */
export interface StreamState {
  status: StatusData | null;
  feed: FeedState;
}

/** État d'avant la première trame. */
export function initialStreamState(): StreamState {
  return { status: null, feed: emptyFeed() };
}

/** Applique une trame au state courant, sans effet de bord. */
export function applyStreamFrame(
  state: StreamState,
  frame: StreamFrame,
): StreamState {
  switch (frame.type) {
    case "hello":
      return {
        status: statusFromHello(frame),
        feed: resetOnHello(state.feed, frame),
      };
    case "status":
      return { status: normalizeStatus(frame.data), feed: state.feed };
    case "trace":
      return { status: state.status, feed: applyTrace(state.feed, frame.data) };
    default:
      // Une trame d'un type inconnu (version future) est ignorée : la page
      // garde l'état et le log qu'elle affiche déjà.
      return state;
  }
}
