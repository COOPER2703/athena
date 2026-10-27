import { describe, expect, it } from "vitest";

import {
  LOGOUT_ALL_MESSAGE,
  MIN_PASSWORD_LENGTH,
  passwordChangeErrorMessage,
  passwordChangePayload,
  validatePasswordChange,
  type PasswordChangeDraft,
} from "./account";
import { UNAUTHORIZED_MESSAGE } from "./auth";

const VALID: PasswordChangeDraft = {
  current: "correct horse battery staple",
  next: "an entirely different passphrase",
  confirmation: "an entirely different passphrase",
};

describe("validatePasswordChange", () => {
  it("exige le mot de passe actuel", () => {
    expect(validatePasswordChange({ ...VALID, current: "" })).toContain(
      "actuel",
    );
  });

  it("impose la longueur minimale au nouveau mot de passe", () => {
    const next = "short";
    expect(
      validatePasswordChange({ ...VALID, next, confirmation: next }),
    ).toContain(String(MIN_PASSWORD_LENGTH));
  });

  it("refuse une confirmation différente", () => {
    expect(
      validatePasswordChange({ ...VALID, confirmation: "autre chose" }),
    ).toBe("Les deux mots de passe ne correspondent pas.");
  });

  it("accepte un changement complet et cohérent", () => {
    expect(validatePasswordChange(VALID)).toBeNull();
  });
});

describe("passwordChangePayload", () => {
  it("ne transmet que les deux champs attendus par l'API", () => {
    expect(passwordChangePayload(VALID)).toEqual({
      current_password: VALID.current,
      new_password: VALID.next,
    });
  });
});

describe("passwordChangeErrorMessage", () => {
  it("nomme un mot de passe actuel incorrect sans ramener au login", () => {
    const wrong = Object.assign(new Error("refusé"), {
      status: 403,
      code: "invalid_password",
    });
    expect(passwordChangeErrorMessage(wrong)).toBe(
      "Le mot de passe actuel est incorrect.",
    );
  });

  it("rappelle la longueur minimale quand le serveur refuse le nouveau", () => {
    const weak = Object.assign(new Error("court"), {
      status: 400,
      code: "weak_password",
    });
    expect(passwordChangeErrorMessage(weak)).toContain(
      String(MIN_PASSWORD_LENGTH),
    );
  });

  it("traite un Ticket Console expiré comme un retour à la connexion", () => {
    const expired = Object.assign(new Error("expiré"), {
      status: 401,
      code: "unauthorized",
    });
    expect(passwordChangeErrorMessage(expired)).toBe(UNAUTHORIZED_MESSAGE);
  });

  it("rend un message réseau quand l'API est injoignable", () => {
    const network = Object.assign(new Error("injoignable"), { status: 0 });
    expect(passwordChangeErrorMessage(network)).toContain(
      "Impossible de joindre",
    );
  });
});

describe("LOGOUT_ALL_MESSAGE", () => {
  it("explique que tous les navigateurs sont déconnectés", () => {
    expect(LOGOUT_ALL_MESSAGE).toContain("tous");
  });
});
