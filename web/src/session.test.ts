import { describe, expect, it } from "vitest";

import {
  activeClientLabel,
  clientDisplayName,
  formatSessionState,
  isActiveClient,
  sessionSummary,
  shortClientId,
} from "./session";

const clients = [
  { client_id: "c-1", client_name: "desktop", platform: "linux" },
  { client_id: "c-2", client_name: "", platform: "android" },
];

describe("formatSessionState", () => {
  it("traduit les états connus du noyau", () => {
    expect(formatSessionState("Idle")).toBe("Inactive");
    expect(formatSessionState("LiveConnecting")).toBe("Connexion Live");
    expect(formatSessionState("LiveActive")).toBe("Live actif");
    expect(formatSessionState("SessionVisible")).toBe("Session en cours");
    expect(formatSessionState("Closing")).toBe("Fermeture");
  });

  it("retombe sur l'état brut quand le noyau en ajoute un", () => {
    expect(formatSessionState("Paused")).toBe("Paused");
    expect(formatSessionState("")).toBe("Inconnu");
  });
});

describe("shortClientId", () => {
  it("tronque les identifiants longs et garde les courts lisibles", () => {
    expect(shortClientId("c-1")).toBe("c-1");
    expect(shortClientId("0123456789abcdef")).toBe("01234567…");
  });
});

describe("clientDisplayName", () => {
  it("préfère le nom, sinon l'identifiant court", () => {
    expect(clientDisplayName(clients[0]!)).toBe("desktop");
    expect(clientDisplayName(clients[1]!)).toBe("c-2");
  });
});

describe("isActiveClient", () => {
  it("reconnaît le Client actif de la Session", () => {
    expect(isActiveClient("c-1", "c-1")).toBe(true);
    expect(isActiveClient("c-2", "c-1")).toBe(false);
  });

  it("ne désigne jamais un Client quand la Session n'en a pas", () => {
    expect(isActiveClient("", "")).toBe(false);
    expect(isActiveClient("c-1", "")).toBe(false);
  });
});

describe("activeClientLabel", () => {  it("préfère le nom du Client actif", () => {
    expect(activeClientLabel("c-1", clients)).toBe("desktop");
  });

  it("montre l'identifiant court quand le nom est inconnu ou vide", () => {
    expect(activeClientLabel("c-2", clients)).toBe("c-2");
    expect(activeClientLabel("0123456789abcdef", clients)).toBe("01234567…");
    expect(activeClientLabel("absent", clients)).toBe("absent");
  });

  it("dit qu'aucun Client n'est actif sans identifiant", () => {
    expect(activeClientLabel("", clients)).toBe("Aucun");
  });
});

describe("sessionSummary", () => {
  it("résume la Session pour l'affichage", () => {
    expect(
      sessionSummary({
        state: "SessionVisible",
        session_id: "sess-1",
        connection_id: "conn-1",
        active_client_id: "c-1",
      }, clients),
    ).toEqual({
      stateLabel: "Session en cours",
      sessionId: "sess-1",
      activeClient: "desktop",
    });
  });

  it("gère une Session sans identifiant ni Client actif", () => {
    expect(
      sessionSummary({
        state: "Idle",
        session_id: "",
        connection_id: "",
        active_client_id: "",
      }, []),
    ).toEqual({
      stateLabel: "Inactive",
      sessionId: "—",
      activeClient: "Aucun",
    });
  });
});
