import { describe, expect, it } from "vitest";

import {
  ApiError,
  describeCode,
  describeError,
  describeFieldErrors,
  errorCode,
  errorStatus,
  isApiError,
  isUnauthorized,
} from "./errors";

describe("ApiError.from", () => {
  it("lit le code stable, le délai de réessai et les erreurs par champ", () => {
    const error = ApiError.from(429, {
      v: 1,
      error: "too_many_attempts",
      message: "Trop de tentatives.",
      retry_after: 42,
      fields: { password: "weak_password" },
    });

    expect(error).toMatchObject({
      status: 429,
      code: "too_many_attempts",
      retryAfter: 42,
      fields: { password: "weak_password" },
    });
  });

  it("rend un code de repli pour un corps qui n'est pas du JSON", () => {
    const error = ApiError.from(502, null);
    expect(error).toMatchObject({ status: 502, code: "http_error" });
    expect(error.fields).toEqual({});
  });

  it("ignore un délai de réessai qui n'est pas un nombre", () => {
    expect(
      ApiError.from(429, { error: "too_many_attempts", retry_after: "60" })
        .retryAfter,
    ).toBeNull();
  });
});

describe("describeError", () => {
  it("traduit chaque code stable de l'API en phrase française", () => {
    const cases: ReadonlyArray<[string, string]> = [
      ["unauthorized", "Ticket Console"],
      ["setup_required", "installation"],
      ["invalid_credentials", "Identifiants"],
      ["already_installed", "Compte"],
      ["invalid_password", "mot de passe actuel"],
      ["weak_password", "mot de passe"],
      ["invalid_username", "nom d'utilisateur"],
      ["too_many_attempts", "Trop de tentatives"],
      ["origin_refused", "Origine"],
      ["invalid_request", "demande"],
      ["auth_disabled", "authentification"],
      ["invalid_token_name", "nom du Jeton"],
      ["invalid_kind", "nature"],
      ["invalid_source", "source"],
      ["token_not_found", "Jeton"],
      ["installation_incomplete", "Installation"],
      ["invalid_secret", "Secret"],
      ["invalid_secret_name", "Secret"],
      ["secret_not_found", "Secret"],
      ["secrets_unavailable", "magasin"],
      ["not_implemented", "pas encore disponible"],
    ];

    for (const [code, fragment] of cases) {
      const message = describeError(new ApiError(400, code));
      expect(message, code).toContain(fragment);
      // Jamais le code brut ni un message serveur.
      expect(message, code).not.toContain(code);
      expect(message, code).not.toContain("_");
    }
  });

  it("donne un délai lisible pour un blocage temporaire", () => {
    const message = describeError(
      new ApiError(429, "too_many_attempts", { retryAfter: 90 }),
    );
    expect(message).toContain("90");
    expect(message).toContain("Trop de tentatives");
  });

  it("nomme une panne réseau plutôt qu'un statut", () => {
    const message = describeError(new ApiError(0, "network"));
    expect(message).toContain("Impossible de joindre");
  });

  it("reste en français pour un code inconnu", () => {
    const message = describeError(new ApiError(418, "code_du_futur"));
    expect(message).toContain("inattendue");
    expect(message).not.toContain("code_du_futur");
  });

  it("reste en français pour une valeur qui n'est pas une erreur d'API", () => {
    expect(describeError(new Error("boom"))).toContain("inattendue");
    expect(describeError(null)).toContain("inattendue");
  });

  it("distingue une panne serveur d'un refus métier", () => {
    expect(describeError(new ApiError(503, "http_error"))).toContain(
      "problème",
    );
  });

  it("traduit les refus de la zone Plugins", () => {
    expect(describeError(new ApiError(404, "plugin_not_found"))).toContain(
      "n'existe pas",
    );
    expect(
      describeError(new ApiError(400, "invalid_plugin_config")),
    ).toContain("configuration");
    expect(describeError(new ApiError(503, "plugins_unavailable"))).toContain(
      "n'est pas disponible",
    );
    expect(describeError(new ApiError(500, "plugins_state_invalid"))).toContain(
      "état des Plugins",
    );
  });

  it("explique un magasin de réglages indisponible sans écran cassé", () => {
    expect(
      describeError(new ApiError(503, "settings_unavailable")),
    ).toContain("magasin de réglages");
    expect(describeError(new ApiError(400, "invalid_settings"))).toContain(
      "réglages",
    );
  });
});

describe("describeFieldErrors", () => {
  it("traduit chaque code de champ en phrase, sans message serveur", () => {
    const error = ApiError.from(400, {
      error: "invalid_request",
      fields: {
        username: "invalid_username",
        password: "weak_password",
      },
    });

    expect(describeFieldErrors(error)).toEqual({
      username: "Le nom d'utilisateur ne peut pas être vide.",
      password: "Le mot de passe est trop court : choisissez-en un plus long.",
    });
  });

  it("rend un objet vide quand l'erreur ne porte pas de champ", () => {
    expect(describeFieldErrors(new Error("boom"))).toEqual({});
  });

  it("traduit les codes de champ de la configuration d'un Plugin", () => {
    const error = ApiError.from(400, {
      error: "invalid_plugin_config",
      fields: { base_url: "invalid_url", typo: "unknown_field" },
    });

    expect(describeFieldErrors(error)).toEqual({
      base_url: "Attendu : une URL http(s) complète.",
      typo: "Ce champ n'est pas déclaré par le manifeste du Plugin.",
    });
  });

  it("affiche la phrase d'un champ de Réglages et jamais un code inconnu", () => {
    const error = ApiError.from(400, {
      error: "invalid_settings",
      fields: {
        recall_k: "La valeur minimale est 1.",
        confirm_url: "Confirmez la nouvelle adresse : http://x.test:9.",
        futur: "code_du_futur",
      },
    });

    expect(describeFieldErrors(error)).toEqual({
      recall_k: "La valeur minimale est 1.",
      confirm_url: "Confirmez la nouvelle adresse : http://x.test:9.",
      futur: "Une erreur inattendue est survenue. Réessayez ; si cela persiste, consultez le journal du serveur.",
    });
  });
});

describe("prédicats d'erreur", () => {
  it("reconnaît une ApiError et son statut", () => {
    const error = new ApiError(401, "unauthorized");
    expect(isApiError(error)).toBe(true);
    expect(errorStatus(error)).toBe(401);
    expect(errorCode(error)).toBe("unauthorized");
  });

  it("reconnaît le refus qui doit ramener à l'écran de connexion", () => {
    expect(isUnauthorized(new ApiError(401, "unauthorized"))).toBe(true);
    expect(isUnauthorized(new ApiError(403, "invalid_password"))).toBe(false);
    expect(isUnauthorized(new Error("réseau"))).toBe(false);
  });

  it("rend des valeurs sûres pour une erreur quelconque", () => {
    expect(isApiError(null)).toBe(false);
    expect(errorStatus(null)).toBeNull();
    expect(errorCode(null)).toBe("");
  });
});

describe("describeCode", () => {
  it("traduit un code de flux SSE sans passer par une réponse HTTP", () => {
    expect(describeCode("busy")).toContain("Athena");
    expect(describeCode("llm_failed")).toContain("modèle");
    expect(describeCode("tour_failed")).toContain("Tour");
    expect(describeCode("invalid_message")).toContain("message");
    expect(describeCode("thread_unavailable")).toContain("fil");
  });

  it("ne fuit jamais un code inconnu", () => {
    expect(describeCode("code_du_futur")).toContain("inattendue");
    expect(describeCode("code_du_futur")).not.toContain("code_du_futur");
  });
});
