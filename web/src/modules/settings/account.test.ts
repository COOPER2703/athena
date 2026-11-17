import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/errors";
import {
  passwordErrorFields,
  passwordErrorMessage,
  validatePasswordChange,
} from "./account";

describe("validatePasswordChange", () => {
  it("accepte une saisie complète et conforme", () => {
    expect(
      validatePasswordChange(
        { current: "ancien", next: "un mot de passe long", confirmation: "un mot de passe long" },
        12,
      ),
    ).toEqual({});
  });

  it("exige chaque champ et la confirmation", () => {
    const fields = validatePasswordChange(
      { current: "", next: "", confirmation: "autre" },
      null,
    );
    expect(Object.keys(fields).sort()).toEqual([
      "confirmation",
      "current_password",
      "new_password",
    ]);
  });

  it("applique la longueur minimale donnée par le serveur", () => {
    const fields = validatePasswordChange(
      { current: "ancien", next: "court", confirmation: "court" },
      12,
    );
    expect(fields.new_password).toContain("12");
  });

  it("ne juge pas la longueur quand le serveur ne la donne pas", () => {
    expect(
      validatePasswordChange(
        { current: "ancien", next: "court", confirmation: "court" },
        null,
      ),
    ).toEqual({});
  });
});

describe("erreurs du serveur", () => {
  it("vise le mot de passe actuel quand il est faux", () => {
    const fields = passwordErrorFields(new ApiError(403, "invalid_password"));
    expect(fields.current_password).toContain("actuel");
    expect(passwordErrorMessage(new ApiError(403, "invalid_password"))).toBeNull();
  });

  it("vise le nouveau mot de passe quand il est trop court", () => {
    const fields = passwordErrorFields(new ApiError(400, "weak_password"));
    expect(fields.new_password).toContain("trop court");
  });

  it("garde un message global pour un code inattendu", () => {
    const error = new ApiError(500, "http_error");
    expect(passwordErrorFields(error)).toEqual({});
    expect(passwordErrorMessage(error)).toContain("serveur");
  });
});
