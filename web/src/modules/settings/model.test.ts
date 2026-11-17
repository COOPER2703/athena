import { describe, expect, it } from "vitest";

import type { SettingField, SettingsPayload } from "../../api/settings";
import { ApiError } from "../../api/errors";
import {
  baselineValue,
  changedKeys,
  confirmUrls,
  consoleUrlAfterRestart,
  discardChanges,
  displayValue,
  draftFromFields,
  pendingFields,
  pendingSummary,
  readOnlyReason,
  resetSetting,
  restartPlan,
  secretSummary,
  settingLabel,
  settingsPayload,
  settingsSections,
  settingsUnavailable,
  sourceLabel,
  sourceTone,
  surfaceChanges,
  surfaceUrl,
  validateSettings,
  writableFields,
} from "./model";

const VOICE: SettingField = {
  key: "voice",
  env: "GEMINI_VOICE",
  kind: "string",
  value: "Leda",
  defaultValue: "Leda",
  source: "default",
  editable: true,
  readOnly: false,
  pending: false,
  pendingValue: null,
  minimum: null,
  maximum: null,
  choices: [],
};

const CONSOLE_PORT: SettingField = {
  key: "console_port",
  env: "ATHENA_CONSOLE_PORT",
  kind: "int",
  value: "8767",
  defaultValue: "8767",
  source: "store",
  editable: true,
  readOnly: false,
  pending: true,
  pendingValue: "8797",
  minimum: 1,
  maximum: 65535,
  choices: [],
};

const CONSOLE_HOST: SettingField = {
  key: "console_host",
  env: "ATHENA_CONSOLE_HOST",
  kind: "string",
  value: "127.0.0.1",
  defaultValue: "127.0.0.1",
  source: "env",
  editable: true,
  readOnly: true,
  pending: false,
  pendingValue: null,
  minimum: null,
  maximum: null,
  choices: [],
};

const SERVER_PORT: SettingField = {
  key: "server_port",
  env: "WS_PORT",
  kind: "int",
  value: "8765",
  defaultValue: "8765",
  source: "default",
  editable: true,
  readOnly: false,
  pending: false,
  pendingValue: null,
  minimum: 1,
  maximum: 65535,
  choices: [],
};

const RECALL_K: SettingField = {
  key: "recall_k",
  env: "ATHENA_RECALL_K",
  kind: "int",
  value: "5",
  defaultValue: "5",
  source: "default",
  editable: true,
  readOnly: false,
  pending: false,
  pendingValue: null,
  minimum: 1,
  maximum: null,
  choices: [],
};

const LOG_LEVEL: SettingField = {
  key: "log_level",
  env: "LOG_LEVEL",
  kind: "string",
  value: "INFO",
  defaultValue: "INFO",
  source: "default",
  editable: true,
  readOnly: false,
  pending: false,
  pendingValue: null,
  minimum: null,
  maximum: null,
  choices: ["DEBUG", "INFO", "WARNING"],
};

const WEBHOOK_ENABLED: SettingField = {
  key: "webhook_enabled",
  env: "ATHENA_WEBHOOK_ENABLED",
  kind: "bool",
  value: "1",
  defaultValue: "1",
  source: "default",
  editable: true,
  readOnly: false,
  pending: false,
  pendingValue: null,
  minimum: null,
  maximum: null,
  choices: [],
};

const RECALL_DISTANCE: SettingField = {
  key: "recall_max_distance",
  env: "ATHENA_RECALL_MAX_DISTANCE",
  kind: "float",
  value: "0.6",
  defaultValue: "0.6",
  source: "default",
  editable: true,
  readOnly: false,
  pending: false,
  pendingValue: null,
  minimum: 0,
  maximum: 2,
  choices: [],
};

const CONSOLIDATION_HOURS: SettingField = {
  key: "consolidation_hours",
  env: "ATHENA_CONSOLIDATION_HOURS",
  kind: "hours",
  value: "0,12",
  defaultValue: "0,12",
  source: "default",
  editable: true,
  readOnly: false,
  pending: false,
  pendingValue: null,
  minimum: null,
  maximum: null,
  choices: [],
};

const MEMORY_DB: SettingField = {
  key: "memory_db",
  env: "ATHENA_MEMORY_DB",
  kind: "string",
  value: "athena_memory.db",
  defaultValue: "athena_memory.db",
  source: "default",
  editable: false,
  readOnly: true,
  pending: false,
  pendingValue: null,
  minimum: null,
  maximum: null,
  choices: [],
};

const FIELDS: readonly SettingField[] = [
  VOICE,
  CONSOLE_HOST,
  CONSOLE_PORT,
  SERVER_PORT,
  RECALL_K,
  LOG_LEVEL,
  WEBHOOK_ENABLED,
  RECALL_DISTANCE,
  CONSOLIDATION_HOURS,
  MEMORY_DB,
  {
    ...VOICE,
    key: "futur_reglage",
    env: "FUTUR",
  },
];

function payload(overrides: Partial<SettingsPayload> = {}): SettingsPayload {
  return {
    fields: FIELDS,
    surfaces: [
      {
        name: "console",
        hostKey: "console_host",
        portKey: "console_port",
        scheme: "http",
        url: "http://127.0.0.1:8767",
      },
      {
        name: "transport",
        hostKey: "server_host",
        portKey: "server_port",
        scheme: "ws",
        url: "ws://0.0.0.0:8765",
      },
    ],
    restart: {
      required: true,
      automatic: false,
      requested: false,
      command: "docker restart athena",
    },
    secrets: [
      { name: "GEMINI_API_KEY", configured: true, updatedAt: 1_760_000_000 },
    ],
    secretsUnreadable: false,
    ...overrides,
  };
}

describe("sections", () => {
  it("range les champs connus dans les quatre sections, puis Avancé", () => {
    const sections = settingsSections(FIELDS);

    expect(sections.map((section) => section.title)).toEqual([
      "Voix",
      "Mémoire",
      "Surfaces",
      "Avancé",
    ]);
    const keys = Object.fromEntries(
      sections.map((section) => [
        section.id,
        section.fields.map((field) => field.key),
      ]),
    );
    expect(keys.voice).toEqual(["voice"]);
    expect(keys.memory).toEqual([
      "recall_k",
      "recall_max_distance",
      "consolidation_hours",
    ]);
    expect(keys.surfaces).toEqual([
      "console_host",
      "console_port",
      "server_port",
      "webhook_enabled",
    ]);
    expect(keys.advanced).toEqual(["log_level", "memory_db", "futur_reglage"]);
  });

  it("n'affiche pas une section vide", () => {
    expect(settingsSections([VOICE]).map((section) => section.id)).toEqual([
      "voice",
    ]);
  });
});

describe("brouillon", () => {
  it("ne retient que les champs écrivables", () => {
    const writable = writableFields(FIELDS);
    expect(writable.map((field) => field.key)).not.toContain("memory_db");
    expect(writable.map((field) => field.key)).not.toContain("console_host");
    expect(writable.map((field) => field.key)).toContain("voice");
  });

  it("part de la valeur stockée quand un changement attend un redémarrage", () => {
    expect(draftFromFields(FIELDS).console_port).toBe("8797");
    expect(baselineValue(CONSOLE_PORT)).toBe("8797");
    expect(baselineValue(VOICE)).toBe("Leda");
    expect(baselineValue({ ...CONSOLE_PORT, pendingValue: null })).toBe("8767");
  });

  it("ne signale aucun changement tant que la saisie vaut la valeur de référence", () => {
    const draft = draftFromFields(FIELDS);
    expect(changedKeys(FIELDS, draft)).toEqual([]);

    expect(
      changedKeys(FIELDS, { ...draft, voice: "  Leda " }),
    ).toEqual([]);
    expect(changedKeys(FIELDS, { ...draft, webhook_enabled: "true" })).toEqual(
      [],
    );
    expect(changedKeys(FIELDS, { ...draft, log_level: "info" })).toEqual([]);
    expect(changedKeys(FIELDS, { ...draft, recall_k: "05" })).toEqual([]);
    expect(changedKeys(FIELDS, { ...draft, recall_max_distance: "0.60" })).toEqual(
      [],
    );
    expect(changedKeys(FIELDS, { ...draft, consolidation_hours: "0, 12" })).toEqual(
      [],
    );
  });

  it("signale chaque champ modifié, jamais un champ en lecture seule", () => {
    const draft = {
      ...draftFromFields(FIELDS),
      voice: "Puck",
      recall_k: "12",
      memory_db: "ailleurs.db",
    };

    expect(changedKeys(FIELDS, draft).sort()).toEqual(["recall_k", "voice"]);
  });

  it("réinitialise un champ vers sa valeur par défaut", () => {
    const draft = { ...draftFromFields(FIELDS), voice: "Puck", console_port: "9000" };

    expect(resetSetting(FIELDS, draft, "voice").voice).toBe("Leda");
    expect(resetSetting(FIELDS, draft, "console_port").console_port).toBe("8767");
    expect(resetSetting(FIELDS, draft, "memory_db").memory_db).toBeUndefined();
  });

  it("abandonne toutes les modifications en revenant au brouillon initial", () => {
    expect(
      discardChanges(FIELDS),
    ).toEqual(draftFromFields(FIELDS));
  });
});

describe("validation", () => {
  const draft = () => draftFromFields(FIELDS);

  it("accepte un brouillon initial", () => {
    expect(validateSettings(FIELDS, draft())).toEqual({});
  });

  it("refuse un entier hors bornes ou illisible", () => {
    expect(validateSettings(FIELDS, { ...draft(), recall_k: "-1" })).toEqual({
      recall_k: "La valeur minimale est 1.",
    });
    expect(validateSettings(FIELDS, { ...draft(), recall_k: "trois" })).toEqual({
      recall_k: "Un nombre entier est attendu.",
    });
    expect(
      validateSettings(FIELDS, { ...draft(), console_port: "70000" }),
    ).toEqual({ console_port: "La valeur maximale est 65535." });
  });

  it("refuse un booléen, un décimal ou une heure non reconnus", () => {
    expect(
      validateSettings(FIELDS, { ...draft(), webhook_enabled: "peut-être" }),
    ).toEqual({ webhook_enabled: "Attendu : « 1 » ou « 0 »." });
    expect(
      validateSettings(FIELDS, { ...draft(), recall_max_distance: "3" }),
    ).toEqual({ recall_max_distance: "La valeur maximale est 2." });
    expect(
      validateSettings(FIELDS, { ...draft(), recall_max_distance: "NaN" }),
    ).toEqual({ recall_max_distance: "Un nombre décimal fini est attendu." });
    expect(
      validateSettings(FIELDS, { ...draft(), consolidation_hours: "25" }),
    ).toEqual({
      consolidation_hours:
        "Attendu : une liste d'heures entre 0 et 23, séparées par des virgules.",
    });
    expect(
      validateSettings(FIELDS, { ...draft(), consolidation_hours: "" }),
    ).toEqual({
      consolidation_hours:
        "Attendu : une liste d'heures entre 0 et 23, séparées par des virgules.",
    });
  });

  it("refuse un hôte qui ressemble à une URL ou contient un espace", () => {
    expect(
      validateSettings(FIELDS, { ...draft(), console_host: "http://x.test" }),
    ).toEqual({});
    // Le champ est en lecture seule : sa valeur n'est jamais validée ni envoyée.
    const mutableHost = { ...CONSOLE_HOST, source: "default" as const, readOnly: false };
    expect(
      validateSettings([mutableHost], { console_host: "http://x.test" }),
    ).toEqual({
      console_host: "Indiquez un hôte seul, sans schéma ni chemin.",
    });
    expect(
      validateSettings([mutableHost], { console_host: "mon hôte" }),
    ).toEqual({ console_host: "Un hôte ne contient pas d'espace." });
  });

  it("refuse une valeur hors des choix déclarés et un texte vide", () => {
    expect(validateSettings(FIELDS, { ...draft(), log_level: "SHOUTY" })).toEqual({
      log_level: "Valeur non reconnue : DEBUG, INFO, WARNING.",
    });
    expect(validateSettings(FIELDS, { ...draft(), voice: "   " })).toEqual({
      voice: "Une valeur est attendue.",
    });
  });

  it("ignore les champs en lecture seule même si le brouillon les porte", () => {
    expect(
      validateSettings(FIELDS, { ...draft(), memory_db: "http://faux" }),
    ).toEqual({});
  });
});

describe("objet envoyé", () => {
  it("porte tous les champs écrivables, canonisés, et rien d'autre", () => {
    const draft = {
      ...draftFromFields(FIELDS),
      voice: "  Puck ",
      recall_k: "05",
      webhook_enabled: "false",
      log_level: "info",
      consolidation_hours: "0, 12",
      memory_db: "ne-part-pas.db",
    };

    const values = settingsPayload(FIELDS, draft);

    expect(values).toEqual({
      voice: "Puck",
      console_port: "8797",
      server_port: "8765",
      recall_k: "5",
      log_level: "INFO",
      webhook_enabled: "0",
      recall_max_distance: "0.6",
      consolidation_hours: "0,12",
      futur_reglage: "Leda",
    });
    expect(values).not.toHaveProperty("console_host");
    expect(values).not.toHaveProperty("memory_db");
  });
});

describe("surfaces et confirmation d'URL", () => {
  it("compose une URL avec schéma, hôte et port, crochets IPv6 compris", () => {
    expect(surfaceUrl("http", "127.0.0.1", "8767")).toBe(
      "http://127.0.0.1:8767",
    );
    expect(surfaceUrl("ws", "::1", "8765")).toBe("ws://[::1]:8765");
    expect(surfaceUrl("ws", "[::1]", "8765")).toBe("ws://[::1]:8765");
  });

  it("décrit chaque surface déplacée avec l'URL actuelle et la nouvelle", () => {
    const draft = { ...draftFromFields(FIELDS), console_port: "8797" };

    expect(surfaceChanges(payload(), draft)).toEqual([
      {
        name: "console",
        scheme: "http",
        currentUrl: "http://127.0.0.1:8767",
        nextUrl: "http://127.0.0.1:8797",
      },
    ]);
    expect(confirmUrls(payload(), draft)).toEqual(["http://127.0.0.1:8797"]);
  });

  it("suit le schéma WebSocket du transport", () => {
    const draft = { ...draftFromFields(FIELDS), server_port: "9000" };

    expect(surfaceChanges(payload({ fields: [SERVER_PORT] }), draft)).toEqual([
      {
        name: "transport",
        scheme: "ws",
        currentUrl: "ws://0.0.0.0:8765",
        nextUrl: "ws://0.0.0.0:9000",
      },
    ]);
    expect(confirmUrls(payload({ fields: [SERVER_PORT] }), draft)).toEqual([
      "ws://0.0.0.0:9000",
    ]);
  });

  it("ne demande aucune confirmation tant que l'adresse ne bouge pas", () => {
    const stable = payload({ fields: [VOICE, RECALL_K] });
    expect(surfaceChanges(stable, draftFromFields(stable.fields))).toEqual([]);
    expect(confirmUrls(stable, draftFromFields(stable.fields))).toEqual([]);
  });

  it("reconfirme une adresse encore en attente de redémarrage", () => {
    // Le serveur compare à la configuration active : tant que le port n'a pas
    // été repris au démarrage, un PUT doit reconfirmer la nouvelle URL.
    const pending = payload();
    expect(
      surfaceChanges(pending, draftFromFields(pending.fields)),
    ).toEqual([
      {
        name: "console",
        scheme: "http",
        currentUrl: "http://127.0.0.1:8767",
        nextUrl: "http://127.0.0.1:8797",
      },
    ]);
  });
});

describe("changements en attente et redémarrage", () => {
  it("compte et résume les champs en attente", () => {
    expect(pendingFields(payload()).map((field) => field.key)).toEqual([
      "console_port",
    ]);
    expect(pendingSummary(payload())).toBe(
      "1 changement, redémarrage requis",
    );

    const many = payload({
      fields: [
        { ...CONSOLE_PORT },
        { ...VOICE, pending: true, pendingValue: "Puck" },
      ],
    });
    expect(pendingSummary(many)).toBe("2 changements, redémarrage requis");
    expect(pendingSummary(payload({ fields: [] }))).toBe("");
  });

  it("rend l'URL que la Console servira après application", () => {
    expect(consoleUrlAfterRestart(payload())).toBe("http://127.0.0.1:8797");
    expect(
      consoleUrlAfterRestart(payload({ fields: [CONSOLE_HOST, VOICE] })),
    ).toBeNull();
    expect(
      consoleUrlAfterRestart(
        payload({
          fields: [
            { ...CONSOLE_PORT, pendingValue: null, defaultValue: "8767" },
          ],
        }),
      ),
    ).toBe("http://127.0.0.1:8767");
  });

  it("choisit comment revenir : rien, attendre, commande, ou nouvelle adresse", () => {
    expect(
      restartPlan(payload({ restart: { ...payload().restart, required: false } }), "http://127.0.0.1:8767"),
    ).toEqual({ kind: "none" });

    expect(
      restartPlan(
        payload({ restart: { ...payload().restart, automatic: false } }),
        "http://127.0.0.1:8767",
      ),
    ).toEqual({ kind: "manual", command: "docker restart athena" });

    expect(
      restartPlan(
        payload({
          fields: [VOICE],
          restart: { ...payload().restart, automatic: true },
        }),
        "http://127.0.0.1:8767",
      ),
    ).toEqual({ kind: "poll" });

    expect(
      restartPlan(
        payload({ restart: { ...payload().restart, automatic: true } }),
        "http://127.0.0.1:8767",
      ),
    ).toEqual({ kind: "moved", url: "http://127.0.0.1:8797" });

    expect(
      restartPlan(
        payload({
          restart: { ...payload().restart, automatic: true },
          fields: [CONSOLE_HOST, VOICE],
        }),
        "http://127.0.0.1:8767/",
      ),
    ).toEqual({ kind: "poll" });
  });
});

describe("libellés et explications", () => {
  it("nomme la source d'une valeur", () => {
    expect(sourceLabel("default")).toBe("Défaut");
    expect(sourceLabel("store")).toBe("Enregistré");
    expect(sourceLabel("env")).toBe("Environnement");
    expect(sourceTone("default")).toBe("neutral");
    expect(sourceTone("store")).toBe("accent");
    expect(sourceTone("env")).toBe("warning");
  });

  it("nomme les champs connus et laisse le reste lisible", () => {
    expect(settingLabel("voice")).toBe("Voix");
    expect(settingLabel("console_port")).toBe("Port de la Console");
    expect(settingLabel("futur_reglage")).toBe("futur_reglage");
  });

  it("explique une lecture seule par l'environnement ou par protection", () => {
    expect(readOnlyReason(CONSOLE_HOST)).toContain("ATHENA_CONSOLE_HOST");
    expect(readOnlyReason(CONSOLE_HOST)).toContain("environnement");
    expect(readOnlyReason(MEMORY_DB)).toContain("Lecture seule");
    expect(readOnlyReason(MEMORY_DB)).toContain("chemin");
  });

  it("affiche un booléen en clair", () => {
    expect(displayValue(WEBHOOK_ENABLED)).toBe("Activé");
    expect(displayValue({ ...WEBHOOK_ENABLED, value: "0" })).toBe("Désactivé");
    expect(displayValue(VOICE)).toBe("Leda");
  });

  it("résume un Secret sans jamais montrer de valeur", () => {
    const formatDate = (seconds: number | null) =>
      seconds === null ? "—" : "12/03/2026 10:00";
    expect(
      secretSummary(
        { name: "GEMINI_API_KEY", configured: true, updatedAt: 42 },
        formatDate,
      ),
    ).toBe("Configuré le 12/03/2026 10:00");
    expect(
      secretSummary(
        { name: "GEMINI_API_KEY", configured: false, updatedAt: null },
        formatDate,
      ),
    ).toBe("Non configuré");
  });

  it("reconnaît un magasin de réglages indisponible", () => {
    expect(
      settingsUnavailable(new ApiError(503, "settings_unavailable")),
    ).toBe(true);
    expect(settingsUnavailable(new ApiError(404, "not_found"))).toBe(true);
    expect(settingsUnavailable(new ApiError(500, "http_error"))).toBe(false);
    expect(settingsUnavailable(new Error("boom"))).toBe(false);
  });
});
