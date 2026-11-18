/**
 * Valeurs partagées avec le serveur de test Python (`e2e/server.py`).
 *
 * Toute modification doit être répercutée des deux côtés : ce sont les
 * libellés d'un contrat de test, pas une configuration de production.
 */

export const E2E_USERNAME = "e2e";
export const E2E_PASSWORD = "athena-e2e-password";
export const E2E_GEMINI_KEY = "e2e-fake-gemini-key";

/** Réponse finale du faux modèle pour un Tour ordinaire. */
export const E2E_REPLY = "Bonjour, je suis Athena. Réponse de test de bout en bout.";

/** Réponse du Tour lent : elle ne doit jamais s'afficher avant l'interruption. */
export const E2E_SLOW_REPLY =
  "Réponse lente : ce texte ne doit pas être affiché.";

/** Message qui demande au faux modèle de ralentir sa réponse finale. */
export const E2E_SLOW_MESSAGE = "Réponds lentement, s'il te plaît.";

/** Premier message d'un Tour ordinaire. */
export const E2E_MESSAGE = "Bonjour Athena, réponds-moi.";

/**
 * Paroles que le faux modèle attribue au micro simulé.
 *
 * Le micro factice de Chromium rejoue `client/tests/data/athena.wav` ; le faux
 * modèle ne l'écoute pas réellement et rend cette transcription d'entrée
 * déterministe, puis la réponse vocale ci-dessous.
 */
export const E2E_VOICE_HEARD = "Bonjour Athena, ceci est un test vocal.";

/** Réponse textuelle et audio du faux modèle à l'audio du micro simulé. */
export const E2E_VOICE_REPLY = "Je vous entends bien. Réponse vocale de test.";

/** Début du motif de refus quand un autre Client tient déjà une Session. */
export const E2E_VOICE_BUSY = "Une autre conversation est déjà en cours.";
