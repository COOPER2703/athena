import { describe, expect, it } from "vitest";

import { ANONYMOUS_SESSION, type AuthSession } from "../auth/session";
import {
  ZONES,
  defaultZone,
  pathForScreen,
  safeReturnPath,
  screenFor,
  zoneById,
  zoneForPath,
} from "./routing";

const AUTHENTICATED: AuthSession = {
  setupRequired: false,
  authenticated: true,
  username: "admin",
};

describe("screenFor", () => {
  it("attend la réponse du serveur avant de choisir", () => {
    expect(screenFor(undefined)).toBe("loading");
  });

  it("impose l'installation tant qu'aucun Compte n'existe", () => {
    expect(screenFor({ ...ANONYMOUS_SESSION, setupRequired: true })).toBe(
      "installation",
    );
    // Un Ticket ne peut pas contourner une installation inachevée.
    expect(screenFor({ ...AUTHENTICATED, setupRequired: true })).toBe(
      "installation",
    );
  });

  it("ouvre l'application avec un Ticket Console valide", () => {
    expect(screenFor(AUTHENTICATED)).toBe("app");
  });

  it("affiche la connexion sinon", () => {
    expect(screenFor(ANONYMOUS_SESSION)).toBe("login");
  });
});

describe("pathForScreen", () => {
  it("donne la route de chaque écran", () => {
    expect(pathForScreen("installation")).toBe("/installation");
    expect(pathForScreen("login")).toBe("/login");
    expect(pathForScreen("app")).toBe("/conversation");
    expect(pathForScreen("loading")).toBe("/");
  });
});

describe("les quatre zones", () => {
  it("sont Conversation, Réglages, Plugins et Diagnostic", () => {
    expect(ZONES.map((zone) => zone.id)).toEqual([
      "conversation",
      "settings",
      "plugins",
      "diagnostic",
    ]);
    expect(ZONES.map((zone) => zone.path)).toEqual([
      "/conversation",
      "/settings",
      "/plugins",
      "/diagnostic",
    ]);
  });

  it("ouvrent sur la Conversation par défaut", () => {
    expect(defaultZone().id).toBe("conversation");
  });

  it("reconnaît une zone et ses sous-routes", () => {
    expect(zoneForPath("/plugins")?.id).toBe("plugins");
    expect(zoneForPath("/settings/voice")?.id).toBe("settings");
    expect(zoneForPath("/plugins/websearch/config")?.id).toBe("plugins");
  });

  it("rend null hors des zones connues", () => {
    expect(zoneForPath("/")).toBeNull();
    expect(zoneForPath("/plugins-bricolage")).toBeNull();
  });

  it("retrouve une zone par son identifiant", () => {
    expect(zoneById("diagnostic")?.path).toBe("/diagnostic");
    expect(zoneById("inconnue")).toBeNull();
  });
});

describe("safeReturnPath", () => {
  it("accepte un chemin interne", () => {
    expect(safeReturnPath("/settings/voice?x=1")).toBe("/settings/voice?x=1");
  });

  it("refuse une sortie vers un autre origine", () => {
    expect(safeReturnPath("//example.com")).toBeNull();
    expect(safeReturnPath("https://example.com")).toBeNull();
    expect(safeReturnPath("/\\example.com")).toBeNull();
    expect(safeReturnPath("settings")).toBeNull();
    expect(safeReturnPath(42)).toBeNull();
    expect(safeReturnPath(undefined)).toBeNull();
  });
});
