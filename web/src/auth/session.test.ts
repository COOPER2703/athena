import { describe, expect, it } from "vitest";

import {
  ANONYMOUS_SESSION,
  normalizeAuth,
  sessionFromLogin,
  sessionFromSetup,
} from "./session";

describe("normalizeAuth", () => {
  it("lit l'état d'Identité et d'Installation de GET /api/session", () => {
    const session = normalizeAuth({
      v: 1,
      setup_required: false,
      authenticated: true,
      username: "admin",
      gemini_configured: true,
      installation_complete: true,
      restart_required: false,
      secrets_unreadable: false,
      password_min_length: 12,
    });

    expect(session).toEqual({
      setupRequired: false,
      authenticated: true,
      username: "admin",
      geminiConfigured: true,
      installationComplete: true,
      restartRequired: false,
      secretsUnreadable: false,
      passwordMinLength: 12,
    });
  });

  it("rend des valeurs sûres pour une réponse vide ou mal typée", () => {
    expect(normalizeAuth(null)).toEqual(ANONYMOUS_SESSION);
    expect(normalizeAuth({ username: 42, password_min_length: "12" })).toEqual(
      ANONYMOUS_SESSION,
    );
  });

  it("ignore une longueur minimale qui n'est pas un entier positif", () => {
    for (const value of [0, -3, 2.5, Number.NaN, "16", null]) {
      expect(
        normalizeAuth({ password_min_length: value }).passwordMinLength,
        String(value),
      ).toBeNull();
    }
  });
});

describe("sessionFromLogin", () => {
  it("ne se croit pas installée tant que l'Installation n'a pas été relue", () => {
    const session = sessionFromLogin({
      v: 1,
      authenticated: true,
      username: "admin",
    });

    expect(session).toMatchObject({
      setupRequired: false,
      authenticated: true,
      username: "admin",
      geminiConfigured: false,
      installationComplete: false,
    });
  });
});

describe("sessionFromSetup", () => {
  it("retient le Compte créé et rend la clé maître, une seule fois", () => {
    const { session, masterKey } = sessionFromSetup({
      v: 1,
      authenticated: true,
      username: "admin",
      master_key: "cle-maitre-jamais-reaffichee",
    });

    expect(session).toMatchObject({
      setupRequired: false,
      authenticated: true,
      username: "admin",
    });
    expect(masterKey).toBe("cle-maitre-jamais-reaffichee");
  });

  it("ne fabrique pas de clé maître quand le serveur n'en rend pas", () => {
    expect(sessionFromSetup({ authenticated: true, username: "admin" }).masterKey)
      .toBeNull();
    expect(sessionFromSetup({ master_key: "   " }).masterKey).toBeNull();
    expect(sessionFromSetup({ master_key: 42 }).masterKey).toBeNull();
  });
});
