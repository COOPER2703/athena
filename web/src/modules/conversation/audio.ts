/**
 * Audio descendant d'un Tour : PCM 16 bits signé mono, 24 kHz.
 *
 * Le serveur envoie le format que le client existant joue déjà (ADR-0024) : le
 * navigateur le convertit tel quel en échantillons flottants, sans inventer un
 * autre format ni passer par un décodeur externe.
 */

/** Fréquence d'échantillonnage imposée par le contrat du flux. */
export const AUDIO_SAMPLE_RATE = 24_000;

/**
 * Convertit des octets PCM 16 bits signés (little-endian) en échantillons
 * flottants entre -1 et 1, la forme qu'attend un `AudioBuffer`.
 *
 * Un octet final orphelin est ignoré : une trame coupée ne doit jamais faire
 * lire hors des octets reçus.
 */
export function pcm16ToFloat32(bytes: Uint8Array): Float32Array<ArrayBuffer> {
  const count = Math.floor(bytes.length / 2);
  const samples = new Float32Array(count);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let index = 0; index < count; index += 1) {
    samples[index] = view.getInt16(index * 2, true) / 32_768;
  }
  return samples;
}

/**
 * Décode la charge base64 d'un événement `audio`.
 *
 * Une charge illisible rend un tableau vide : un Tour ne s'arrête jamais parce
 * qu'un morceau d'audio est corrompu, le texte reste la réponse de référence.
 */
export function decodeBase64(value: string): Uint8Array {
  if (value === "") {
    return new Uint8Array();
  }
  try {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  } catch {
    return new Uint8Array();
  }
}
