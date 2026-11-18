import { describe, expect, it } from "vitest";

import {
  activeClientLabel,
  clientDisplayName,
  emptySnapshot,
  formatPluginState,
  formatSessionState,
  installationIssues,
  isActiveClient,
  isPluginState,
  normalizeSnapshot,
  pluginStateTone,
  sessionStateTone,
  sessionSummary,
  shortClientId,
  type DiagnosticSnapshot,
} from "./model";

const FULL = {
  session: {
    state: "SessionVisible",
    session_id: "s1",
    connection_id: "s1",
    active_client_id: "c1",
  },
  clients: [
    { client_id: "c1", client_name: "bureau", platform: "linux" },
  ],
  plugins: [
    { name: "fixture", state: "activated", tools: ["echo"] },
    { name: "casse", state: "failed", tools: [] },
  ],
  config: {
    model: "gemini-live",
    voice: "Leda",
    user_id: "owner",
    memory_db: "memory.db",
    log_dir: "logs",
    version: "0.1.0",
    api_key: 42,
  },
  installation: {
    account_exists: true,
    gemini_configured: true,
    complete: true,
    secrets_unreadable: false,
    surfaces: { gemini: true, transport: true, webhook: false },
  },
  restart_required: false,
  health: {
    version: "0.1.0",
    uptime_seconds: 3600,
    last_consolidation: {
      at: 1_700_000_000,
      facts_extracted: 4,
      facts_stored: 3,
      duplicates: 1,
      purged: 2,
      transcript_chars: 800,
    },
    databases: { memory: 1024, auth: 2048, settings: 0 },
    last_error: {
      timestamp: 1_700_000_123,
      source: "gemini",
      kind: "LlmFailed",
      level: "error",
    },
    trace_buffer: { size: 12, capacity: 500 },
  },
};

describe("normalizeSnapshot", () => {
  it("lit l'instantané étendu sans rien perdre", () => {
    const snapshot = normalizeSnapshot(FULL);

    expect(snapshot.session).toEqual({
      state: "SessionVisible",
      sessionId: "s1",
      connectionId: "s1",
      activeClientId: "c1",
    });
    expect(snapshot.clients).toEqual([
      { clientId: "c1", clientName: "bureau", platform: "linux" },
    ]);
    expect(snapshot.plugins[1]).toEqual({
      name: "casse",
      state: "failed",
      tools: [],
    });
    expect(snapshot.installation).toEqual({
      accountExists: true,
      geminiConfigured: true,
      complete: true,
      secretsUnreadable: false,
      surfaces: { gemini: true, transport: true, webhook: false },
    });
    expect(snapshot.restartRequired).toBe(false);
    expect(snapshot.health.uptimeSeconds).toBe(3600);
    expect(snapshot.health.lastConsolidation).toEqual({
      at: 1_700_000_000,
      factsExtracted: 4,
      factsStored: 3,
      duplicates: 1,
      purged: 2,
    });
    expect(snapshot.health.databases).toEqual({
      memory: 1024,
      auth: 2048,
      settings: 0,
    });
    expect(snapshot.health.lastError).toEqual({
      timestamp: 1_700_000_123,
      source: "gemini",
      kind: "LlmFailed",
      level: "error",
    });
    expect(snapshot.health.traceBuffer).toEqual({ size: 12, capacity: 500 });
  });

  it("ne recopie que les chaînes d'une configuration inconnue", () => {
    const snapshot = normalizeSnapshot(FULL);

    expect(snapshot.config).not.toHaveProperty("api_key");
    expect(snapshot.config.model).toBe("gemini-live");
  });

  it("retombe sur un état vide pour un instantané absent ou mal formé", () => {
    expect(normalizeSnapshot(undefined)).toEqual(emptySnapshot());
    expect(normalizeSnapshot("nope")).toEqual(emptySnapshot());
    expect(normalizeSnapshot({ clients: "pas une liste" }).clients).toEqual([]);
  });

  it("tolère un instantané sans les sections d'Installation et de santé", () => {
    const legacy = normalizeSnapshot({ session: { state: "Idle" } });

    expect(legacy.installation.complete).toBe(false);
    expect(legacy.health.traceBuffer).toBeNull();
    expect(legacy.health.lastConsolidation).toBeNull();
    expect(legacy.health.lastError).toBeNull();
    expect(legacy.restartRequired).toBe(false);
  });

  it("borne les mesures à des valeurs positives", () => {
    const snapshot = normalizeSnapshot({
      health: {
        uptime_seconds: -10,
        databases: { memory: -1, auth: "x" },
        last_consolidation: { at: 12, facts_stored: -3 },
        trace_buffer: { size: -4, capacity: 500 },
      },
    });

    expect(snapshot.health.uptimeSeconds).toBe(0);
    expect(snapshot.health.databases).toEqual({
      memory: 0,
      auth: 0,
      settings: 0,
    });
    expect(snapshot.health.lastConsolidation?.factsStored).toBe(0);
    expect(snapshot.health.traceBuffer).toEqual({ size: 0, capacity: 500 });
  });
});

describe("libellés de Session et de Clients", () => {
  it("traduit les états connus et laisse passer un état inconnu", () => {
    expect(formatSessionState("Idle")).toBe("Inactive");
    expect(formatSessionState("SessionVisible")).toBe("Session en cours");
    expect(formatSessionState("TourEnCours")).toBe("TourEnCours");
    expect(formatSessionState("")).toBe("Inconnu");
  });

  it("donne la tonalité d'un état sans couleur pour l'inconnu", () => {
    expect(sessionStateTone("SessionVisible")).toBe("accent");
    expect(sessionStateTone("LiveConnecting")).toBe("warning");
    expect(sessionStateTone("Idle")).toBe("neutral");
    expect(sessionStateTone("Futur")).toBe("neutral");
  });

  it("raccourcit un identifiant trop long et préfère le nom", () => {
    expect(shortClientId("court")).toBe("court");
    expect(shortClientId("un-identifiant-tres-long")).toBe("un-ident…");
    expect(
      clientDisplayName({ clientId: "abc", clientName: "", platform: "" }),
    ).toBe("abc");
    expect(
      clientDisplayName({ clientId: "abc", clientName: "bureau", platform: "" }),
    ).toBe("bureau");
  });

  it("désigne le Client propriétaire, sinon « Aucun »", () => {
    const clients = [
      { clientId: "c1", clientName: "bureau", platform: "linux" },
    ];
    expect(isActiveClient("c1", "c1")).toBe(true);
    expect(isActiveClient("", "")).toBe(false);
    expect(activeClientLabel("c1", clients)).toBe("bureau");
    expect(activeClientLabel("c2", clients)).toBe("c2");
    expect(activeClientLabel("", clients)).toBe("Aucun");
  });

  it("résume la Session pour le panneau", () => {
    const snapshot: DiagnosticSnapshot = normalizeSnapshot(FULL);
    expect(sessionSummary(snapshot.session, snapshot.clients)).toEqual({
      stateLabel: "Session en cours",
      sessionId: "s1",
      activeClient: "bureau",
    });
  });
});

describe("libellés de Plugin", () => {
  it("traduit les quatre états du registre", () => {
    expect(formatPluginState("activated")).toBe("Activé");
    expect(formatPluginState("disabled")).toBe("Désactivé");
    expect(formatPluginState("missing")).toBe("Manquant");
    expect(formatPluginState("failed")).toBe("En échec");
    expect(formatPluginState("futur")).toBe("futur");
    expect(formatPluginState("")).toBe("Inconnu");
  });

  it("borne la tonalité aux états connus", () => {
    expect(isPluginState("failed")).toBe(true);
    expect(isPluginState("constructor")).toBe(false);
    expect(pluginStateTone("activated")).toBe("accent");
    expect(pluginStateTone("failed")).toBe("danger");
    expect(pluginStateTone("missing")).toBe("warning");
    expect(pluginStateTone("futur")).toBe("neutral");
  });
});

describe("manques d'Installation", () => {
  it("ne signale rien quand tout est complet", () => {
    expect(installationIssues(normalizeSnapshot(FULL))).toEqual([]);
  });

  it("nomme une clé Gemini absente sans masquer les mesures", () => {
    const snapshot = normalizeSnapshot({
      ...FULL,
      installation: {
        account_exists: true,
        gemini_configured: false,
        complete: false,
        secrets_unreadable: false,
        surfaces: { gemini: false, transport: false, webhook: false },
      },
    });

    const issues = installationIssues(snapshot);
    expect(issues.map((issue) => issue.title)).toEqual([
      "Clé Gemini manquante",
    ]);
    expect(issues[0]?.tone).toBe("warning");
  });

  it("distingue les Secrets illisibles et le redémarrage en attente", () => {
    const snapshot = normalizeSnapshot({
      ...FULL,
      restart_required: true,
      installation: {
        account_exists: true,
        gemini_configured: true,
        complete: true,
        secrets_unreadable: true,
        surfaces: { gemini: false, transport: false, webhook: false },
      },
    });

    const issues = installationIssues(snapshot);
    expect(issues.map((issue) => issue.title)).toEqual([
      "Secrets illisibles",
      "Redémarrage requis",
    ]);
    expect(issues[0]?.tone).toBe("danger");
  });

  it("signale l'absence de Compte avant tout le reste", () => {
    const snapshot = normalizeSnapshot({
      installation: { account_exists: false },
    });
    expect(installationIssues(snapshot)[0]?.title).toBe("Aucun Compte");
  });
});
