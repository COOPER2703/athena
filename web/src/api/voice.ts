/**
 * WebSocket de la conversation vocale, authentifiée par le Ticket Console.
 *
 * Le navigateur ne parle ni Protobuf ni Jeton : il envoie des trames binaires
 * PCM 16 bits mono 16 kHz et reçoit des trames JSON versionnées (ADR-0027).
 * Ce module ne décide de rien : il décode et transmet, la machine à états
 * (`voice.ts`) et le fournisseur décident quand ouvrir le micro et quand
 * reconnecter.
 */

import {
  decodeVoiceFrame,
  type VoiceEndReason,
  type VoiceRole,
} from "../modules/conversation/voice";

/** Événements du socket, déjà décodés. */
export interface VoiceHandlers {
  /** La Connexion Live est ouverte : seul moment où le micro peut s'ouvrir. */
  onReady(): void;
  onTranscript(role: VoiceRole, text: string): void;
  onAudio(pcm: Uint8Array): void;
  onEnded(reason: VoiceEndReason): void;
  onRefused(code: string, message: string): void;
  /** Le socket s'est refermé, quelle qu'en soit la cause. */
  onClose(): void;
}

/** Poignée d'une connexion ouverte : envoi audio et fermeture volontaire. */
export interface VoiceConnection {
  /** Envoie une trame PCM ; un socket non ouvert la laisse tomber. */
  send(pcm: Uint8Array): void;
  /** Ferme volontairement : le serveur termine la Session (CLIENT_GONE). */
  close(): void;
}

/**
 * Identifiant de cet **onglet**, tiré une fois par chargement de page.
 *
 * Il ne sert qu'à reconnaître sa propre reconnexion : le serveur remplace
 * alors son Client Console abandonné, sans jamais voler la Session d'un second
 * onglet, qui présente le sien.
 */
const TAB_ID = newTabId();

function newTabId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** URL du WebSocket vocal, sur la même origine que la page servie. */
export function voiceUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const tab = encodeURIComponent(TAB_ID);
  return `${protocol}//${window.location.host}/api/voice?tab=${tab}`;
}

/**
 * Ouvre la conversation vocale et branche les événements.
 *
 * Aucune trame montante n'a de contrôle : fermer le socket est la seule façon
 * de terminer, et le serveur la traduit en `CLIENT_GONE`. Un serveur qui envoie
 * `ended` ou `refused` referme derrière lui ; l'appelant distingue une fin
 * annoncée d'une coupure grâce à la machine à états.
 */
export function connectVoice(handlers: VoiceHandlers): VoiceConnection {
  const socket = new WebSocket(voiceUrl());
  socket.binaryType = "arraybuffer";

  socket.addEventListener("message", (event) => {
    if (typeof event.data !== "string") {
      // Le descendant est du JSON ; du binaire inattendu est ignoré.
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(event.data);
    } catch {
      return;
    }
    const frame = decodeVoiceFrame(parsed);
    if (frame === null) {
      return;
    }
    switch (frame.type) {
      case "ready":
        handlers.onReady();
        return;
      case "transcript":
        handlers.onTranscript(frame.role, frame.text);
        return;
      case "audio":
        handlers.onAudio(frame.pcm);
        return;
      case "ended":
        handlers.onEnded(frame.reason);
        return;
      case "refused":
        handlers.onRefused(frame.code, frame.message);
        return;
    }
  });

  socket.addEventListener("close", () => {
    handlers.onClose();
  });

  return {
    send(pcm: Uint8Array): void {
      if (pcm.byteLength > 0 && socket.readyState === WebSocket.OPEN) {
        // Copie dans un tampon propre : `send` n'accepte pas un tableau dont
        // le tampon peut être partagé, et la trame du Worklet est transférée.
        const bytes = new Uint8Array(pcm.byteLength);
        bytes.set(pcm);
        socket.send(bytes);
      }
    },
    close(): void {
      try {
        socket.close();
      } catch {
        // Un socket déjà fermé refuse de se fermer : rien à faire.
      }
    },
  };
}
