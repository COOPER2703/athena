import { describe, expect, it } from "vitest";

import {
  UNAUTHORIZED_MESSAGE,
  authErrorMessage,
  isUnauthorized,
  normalizeAuth,
  screenForAuth,
  type AuthSession,
} from "./auth";

const SESSION: AuthSession = {
  setupRequired: false,
  authenticated: false,
  username: null,
};

describe("normalizeAuth", () => {
  it("rend un état sûr pour une réponse vide", () => {
    expect(normalizeAuth({})).toEqual({
      setupRequired: false,
      authenticated: false,
      username: null,
    });
    expect(normalizeAuth(null)).toEqual({
      setupRequired: false,
      authenticated: false,
      username: null,
    });
  });

  it("lit l'installation requise et l'identité connectée", () => {
    expect(normalizeAuth({ setup_required: true })).toEqual({
      setupRequired: true,
      authenticated: false,
      username: null,
    });
    expect(
      normalizeAuth({
        setup_required: false,
        authenticated: true,
        username: "admin",
      }),
    ).toEqual({
      setupRequired: false,
      authenticated: true,
      username: "admin",
    });
  });

  it("ignore un nom d'utilisateur qui n'est pas une chaîne non vide", () => {
    expect(normalizeAuth({ authenticated: true, username: 42 }).username).toBe(
      null,
    );
    expect(normalizeAuth({ authenticated: true, username: "" }).username).toBe(
      null,
    );
  });
});

describe("screenForAuth", () => {
  it("impose l'installation tant qu'aucun Compte n'existe", () => {
    expect(screenForAuth({ ...SESSION, setupRequired: true })).toBe("setup");
  });

  it("ouvre la console avec un Ticket Console valide", () => {
    expect(
      screenForAuth({ ...SESSION, authenticated: true, username: "admin" }),
    ).toBe("console");
  });

  it("affiche la connexion sinon", () => {
    expect(screenForAuth(SESSION)).toBe("login");
  });
});

describe("isUnauthorized", () => {
  it("reconnaît un refus 401 porté par une ApiError", () => {
    const error = Object.assign(new Error("expiré"), {
      status: 401,
      code: "unauthorized",
    });
    expect(isUnauthorized(error)).toBe(true);
    expect(isUnauthorized(Object.assign(new Error("interdit"), { status: 403 }))).toBe(
      false,
    );
    expect(isUnauthorized(new Error("réseau"))).toBe(false);
    expect(isUnauthorized(null)).toBe(false);
  });
});

describe("authErrorMessage", () => {
  it("ne distingue pas un utilisateur inconnu d'un mot de passe faux", () => {
    const wrong = Object.assign(new Error("bad"), {
      status: 401,
      code: "invalid_credentials",
    });
    expect(authErrorMessage(wrong)).toBe("Identifiants incorrects.");
  });

  it("affiche le délai d'un blocage 429", () => {
    const locked = Object.assign(new Error("locked"), {
      status: 429,
      code: "too_many_attempts",
      retryAfter: 900,
    });
    expect(authErrorMessage(locked)).toContain("900");
  });

  it("préfère le message du serveur pour une erreur métier", () => {
    const weak = Object.assign(new Error("Le mot de passe doit contenir au moins 12 caractères."), {
      status: 400,
      code: "weak_password",
    });
    expect(authErrorMessage(weak)).toBe(
      "Le mot de passe doit contenir au moins 12 caractères.",
    );
  });

  it("oriente vers la connexion quand l'instance est déjà installée", () => {
    const installed = Object.assign(new Error("déjà installé"), {
      status: 409,
      code: "already_installed",
    });
    expect(authErrorMessage(installed)).toBe(
      "Un Compte existe déjà : connectez-vous.",
    );
  });

  it("rend un message réseau pour une panne de transport", () => {
    const network = Object.assign(new Error("injoignable"), { status: 0 });
    expect(authErrorMessage(network)).toContain("Impossible de joindre");
    expect(authErrorMessage(new Error("inconnu"))).toContain(
      "Impossible de joindre",
    );
  });

  it("nomme l'expiration d'un Ticket Console", () => {
    expect(UNAUTHORIZED_MESSAGE).toContain("Ticket Console");
  });
});
