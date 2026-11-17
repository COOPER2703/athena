import type { Page } from "@playwright/test";

/**
 * Sonde Web Audio des parcours navigateur.
 *
 * Le navigateur ne laisse pas un test observer un haut-parleur. On instrumente
 * donc `AudioBufferSourceNode.start`, le seul point que le lecteur de la
 * Conversation appelle quand il joue réellement un morceau : un `start` prouve
 * que l'audio a été décodé, mis en tampon et lancé, pas seulement reçu du flux.
 */

interface AudioProbe {
  starts: number;
  durations: number[];
}

declare global {
  interface Window {
    __athenaAudioProbe?: AudioProbe;
  }
}

/** À appeler avant le premier `goto`, dans le contexte du parcours. */
export async function installAudioProbe(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const probe: AudioProbe = { starts: 0, durations: [] };
    Object.defineProperty(window, "__athenaAudioProbe", {
      value: probe,
      configurable: true,
    });
    const original = AudioBufferSourceNode.prototype.start;
    AudioBufferSourceNode.prototype.start = function patchedStart(
      this: AudioBufferSourceNode,
      when?: number,
      offset?: number,
      duration?: number,
    ): void {
      probe.starts += 1;
      probe.durations.push(this.buffer?.duration ?? 0);
      original.call(this, when, offset, duration);
    };
  });
}

/** Nombre de sources audio effectivement démarrées dans la page. */
export async function audioStarts(page: Page): Promise<number> {
  return page.evaluate(() => window.__athenaAudioProbe?.starts ?? 0);
}
