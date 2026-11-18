import { describe, expect, it } from "vitest";

import { ApiError } from "../api/errors";
import { isRetryable } from "./queryClient";

describe("politique de réessai", () => {
  it("réessaie une panne réseau ou serveur", () => {
    expect(isRetryable(0, new ApiError(0, "network"))).toBe(true);
    expect(isRetryable(0, new ApiError(503, "http_error"))).toBe(true);
    expect(isRetryable(1, new ApiError(503, "http_error"))).toBe(true);
  });

  it("abandonne après deux réessais", () => {
    expect(isRetryable(2, new ApiError(0, "network"))).toBe(false);
  });

  it("ne réessaie jamais un refus métier", () => {
    expect(isRetryable(0, new ApiError(401, "unauthorized"))).toBe(false);
    expect(isRetryable(0, new ApiError(404, "not_found"))).toBe(false);
    expect(isRetryable(0, new ApiError(429, "too_many_attempts"))).toBe(false);
    expect(isRetryable(0, new Error("inconnue"))).toBe(false);
  });
});
