import { describe, expect, it } from "vitest";

import { ApiError } from "../api/errors";
import { ANONYMOUS_SESSION, type AuthSession } from "../auth/session";
import {
  GEMINI_SETTINGS_PATH,
  INSTALL_STEPS,
  accountErrorFields,
  geminiFieldError,
  installationGaps,
  isConversationAvailable,
  nextStep,
  startStep,
  validateAccount,
} from "./installation";

const ANONYMOUS: AuthSession = { ...ANONYMOUS_SESSION, setupRequired: true };

const INCOMPLETE: AuthSession = {
  ...ANONYMOUS_SESSION,
  authenticated: true,
  username: "admin",
};

const COMPLETE: AuthSession = {
  ...INCOMPLETE,
  geminiConfigured: true,
  installationComplete: true,
};

describe("startStep", () => {
  it("commence par le Compte tant qu'aucun Compte n'existe", () => {
    expect(startStep(ANONYMOUS)).toBe("account");
  });

  it("reprend à la clé Gemini quand le Compte existe déjà", () => {
    expect(startStep(INCOMPLETE)).toBe("gemini");
  });

  it("ne rouvre jamais le parcours d'une Installation terminée", () => {
    expect(startStep(COMPLETE)).toBeNull();
  });
});

describe("nextStep", () => {
  it("suit Compte, clé maître, clé Gemini, voix et profil, récapitulatif", () => {
    expect(nextStep("account")).toBe("master-key");
    expect(nextStep("master-key")).toBe("gemini");
    expect(nextStep("gemini")).toBe("voice");
    expect(nextStep("voice")).toBe("summary");
    expect(nextStep("summary")).toBeNull();
  });

  it("annonce quatre étapes numérotées, la clé maître restant un interlude", () => {
    expect(INSTALL_STEPS).toEqual(["account", "gemini", "voice", "summary"]);
  });
});

describe("validateAccount", () => {
  const draft = {
    username: "admin",
    password: "mot-de-passe-solide",
    confirmation: "mot-de-passe-solide",
  };

  it("accepte une saisie complète et conforme", () => {
    expect(validateAccount(draft, 12)).toEqual({});
  });

  it("refuse un nom vide, en le réduisant à ses espaces", () => {
    expect(
      validateAccount({ ...draft, username: "   " }, 12).username,
    ).toContain("nom");
  });

  it("explique la longueur minimale avant tout appel serveur", () => {
    const fields = validateAccount({ ...draft, password: "court", confirmation: "court" }, 12);
    expect(fields.password).toContain("12");
  });

  it("n'applique aucune longueur quand le serveur ne l'a pas dite", () => {
    expect(
      validateAccount({ ...draft, password: "court", confirmation: "court" }, null),
    ).toEqual({});
  });

  it("exige la même saisie dans les deux champs de mot de passe", () => {
    expect(
      validateAccount({ ...draft, confirmation: "autre-chose" }, 12).confirmation,
    ).toContain("correspondent");
  });
});

describe("accountErrorFields", () => {
  it("place un mot de passe trop court sous le champ du mot de passe", () => {
    const fields = accountErrorFields(new ApiError(400, "weak_password"));
    expect(fields.password).toContain("mot de passe");
  });

  it("place un nom refusé sous le champ du nom", () => {
    const fields = accountErrorFields(new ApiError(400, "invalid_username"));
    expect(fields.username).toContain("nom");
  });

  it("traduit aussi les erreurs par champ envoyées par le serveur", () => {
    const fields = accountErrorFields(
      ApiError.from(400, {
        error: "invalid_request",
        fields: { password: "weak_password" },
      }),
    );
    expect(fields.password).toContain("mot de passe");
  });

  it("laisse une erreur non attribuable au bandeau de l'écran", () => {
    expect(accountErrorFields(new ApiError(409, "already_installed"))).toEqual(
      {},
    );
    expect(accountErrorFields(new Error("réseau"))).toEqual({});
  });
});

describe("geminiFieldError", () => {
  it("attribue une clé refusée au champ de saisie", () => {
    const message = geminiFieldError(new ApiError(400, "invalid_secret"));
    expect(message).toContain("clé Gemini");
    expect(message).not.toContain("invalid_secret");
  });

  it("laisse les autres pannes au bandeau de l'écran", () => {
    expect(geminiFieldError(new ApiError(503, "secrets_unavailable"))).toBeNull();
    expect(geminiFieldError(new ApiError(0, "network"))).toBeNull();
    expect(geminiFieldError(null)).toBeNull();
  });
});

describe("installationGaps", () => {
  it("nomme la clé Gemini manquante et les surfaces fermées", () => {
    const [gap, ...rest] = installationGaps(INCOMPLETE);
    expect(rest).toEqual([]);
    expect(gap).toMatchObject({ id: "gemini" });
    expect(gap?.body).toContain("transport");
    expect(gap?.body).toContain("Webhook");
  });

  it("signale des Secrets illisibles sans rien réinitialiser", () => {
    const gaps = installationGaps({ ...INCOMPLETE, secretsUnreadable: true });
    expect(gaps.map((gap) => gap.id)).toEqual(["gemini", "secrets"]);
  });

  it("signale un redémarrage requis pour la clé déjà enregistrée", () => {
    const gaps = installationGaps({
      ...COMPLETE,
      restartRequired: true,
    });
    expect(gaps.map((gap) => gap.id)).toEqual(["restart"]);
  });

  it("ne réclame rien une fois l'Installation terminée", () => {
    expect(installationGaps(COMPLETE)).toEqual([]);
  });
});

describe("isConversationAvailable", () => {
  it("ferme la Conversation tant qu'aucune clé Gemini utilisable n'existe", () => {
    expect(isConversationAvailable(INCOMPLETE)).toBe(false);
    expect(isConversationAvailable(undefined)).toBe(false);
  });

  it("la ferme aussi quand la clé enregistrée n'est pas encore chargée", () => {
    expect(
      isConversationAvailable({ ...COMPLETE, restartRequired: true }),
    ).toBe(false);
  });

  it("l'ouvre dès que la clé est utilisable", () => {
    expect(isConversationAvailable(COMPLETE)).toBe(true);
  });
});

describe("GEMINI_SETTINGS_PATH", () => {
  it("mène au réglage manquant, dans la zone Réglages", () => {
    expect(GEMINI_SETTINGS_PATH.startsWith("/settings")).toBe(true);
  });
});
