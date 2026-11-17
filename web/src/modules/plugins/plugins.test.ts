import { describe, expect, it } from "vitest";

import type { PluginConfigField } from "../../api/plugins";
import {
  configChanged,
  configDraft,
  configPayload,
  isHttpUrl,
  platformLabel,
  pluginStateLabel,
  pluginStateTone,
  secretSummary,
  validateConfig,
} from "./plugins";

const FIELDS: readonly PluginConfigField[] = [
  { key: "base_url", type: "url", required: true, value: "https://x.test" },
  { key: "room", type: "string", required: false, value: "" },
];

describe("libellés", () => {
  it("traduit les quatre états du registre", () => {
    expect(pluginStateLabel("activated")).toBe("Activé");
    expect(pluginStateLabel("disabled")).toBe("Désactivé");
    expect(pluginStateLabel("failed")).toBe("En échec");
    expect(pluginStateLabel("missing")).toBe("Manquant");
  });

  it("réserve l'accent à l'état actif", () => {
    expect(pluginStateTone("activated")).toBe("accent");
    expect(pluginStateTone("disabled")).toBe("neutral");
    expect(pluginStateTone("failed")).toBe("danger");
    expect(pluginStateTone("missing")).toBe("warning");
  });

  it("traduit les plateformes connues et laisse le reste lisible", () => {
    expect(platformLabel("server")).toBe("Serveur");
    expect(platformLabel("desktop")).toBe("Bureau");
    expect(platformLabel("")).toBe("Inconnue");
    expect(platformLabel("futur")).toBe("futur");
  });

  it("résume un Secret sans jamais montrer de valeur", () => {
    const formatDate = (seconds: number | null) =>
      seconds === null ? "—" : "12/03/2026 10:00";
    expect(
      secretSummary(
        {
          key: "token",
          required: true,
          variable: "ATHENA_PLUGIN_ALPHA_TOKEN",
          configured: true,
          updatedAt: 42,
        },
        formatDate,
      ),
    ).toBe("Configuré le 12/03/2026 10:00");
    expect(
      secretSummary(
        {
          key: "token",
          required: false,
          variable: "ATHENA_PLUGIN_ALPHA_TOKEN",
          configured: false,
          updatedAt: null,
        },
        formatDate,
      ),
    ).toBe("Non configuré");
  });
});

describe("formulaire de configuration", () => {
  it("prépare un brouillon depuis les valeurs du manifeste", () => {
    expect(configDraft(FIELDS)).toEqual({
      base_url: "https://x.test",
      room: "",
    });
  });

  it("exige un champ requis vide", () => {
    expect(validateConfig(FIELDS, { base_url: "  ", room: "" })).toEqual({
      base_url: "Ce champ est obligatoire.",
    });
  });

  it("refuse une URL sans schéma http(s) ni hôte", () => {
    expect(validateConfig(FIELDS, { base_url: "x.test", room: "" })).toEqual({
      base_url: "Attendu : une URL http(s) complète.",
    });
    expect(validateConfig(FIELDS, { base_url: "https://x.test/a", room: "" })).toEqual(
      {},
    );
  });

  it("n'envoie que les champs déclarés, élagués", () => {
    expect(
      configPayload(FIELDS, {
        base_url: "  https://x.test ",
        room: " salon ",
        inconnu: " ignoré ",
      }),
    ).toEqual({ base_url: "https://x.test", room: "salon" });
  });

  it("détecte un brouillon modifié", () => {
    expect(configChanged(FIELDS, configDraft(FIELDS))).toBe(false);
    expect(
      configChanged(FIELDS, { base_url: "https://y.test", room: "" }),
    ).toBe(true);
  });
});

describe("isHttpUrl", () => {
  it("accepte http et https avec un hôte", () => {
    expect(isHttpUrl("http://homeassistant.local:8123")).toBe(true);
    expect(isHttpUrl("https://api.test/chemin")).toBe(true);
  });

  it("refuse un schéma ou un hôte absent", () => {
    expect(isHttpUrl("homeassistant.local:8123")).toBe(false);
    expect(isHttpUrl("ftp://x.test")).toBe(false);
    expect(isHttpUrl("https://")).toBe(false);
    expect(isHttpUrl("")).toBe(false);
  });
});
