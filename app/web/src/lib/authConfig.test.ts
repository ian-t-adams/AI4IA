import { afterEach, describe, expect, it } from "vitest";

import { getAuthConfig } from "./authConfig";

const AUTH_ENV_KEYS = [
  "WEB_AUTH_PROVIDER",
  "ENTRA_CLIENT_ID",
  "ENTRA_TENANT_ID",
  "ENTRA_API_SCOPE",
  "ENTRA_REDIRECT_URI",
] as const;
const originalEnv = Object.fromEntries(
  AUTH_ENV_KEYS.map((key) => [key, process.env[key]]),
);

afterEach(() => {
  for (const key of AUTH_ENV_KEYS) {
    const value = originalEnv[key];
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
});

describe("getAuthConfig", () => {
  it("preserves the local development default when Entra was not requested", () => {
    for (const key of AUTH_ENV_KEYS) delete process.env[key];

    expect(getAuthConfig()).toEqual({
      provider: "dev",
      clientId: "",
      tenantId: "",
      apiScope: "",
    });
  });

  it("fails closed and names every missing value when Entra was requested", () => {
    process.env.WEB_AUTH_PROVIDER = "entra";
    process.env.ENTRA_CLIENT_ID = "client";
    delete process.env.ENTRA_TENANT_ID;
    delete process.env.ENTRA_API_SCOPE;

    expect(getAuthConfig()).toEqual({
      provider: "configuration-error",
      missingValues: ["ENTRA_TENANT_ID", "ENTRA_API_SCOPE"],
    });
  });

  it("returns Entra configuration only when all required values exist", () => {
    process.env.WEB_AUTH_PROVIDER = "ENTRA";
    process.env.ENTRA_CLIENT_ID = "client";
    process.env.ENTRA_TENANT_ID = "tenant";
    process.env.ENTRA_API_SCOPE = "api://client/access";
    process.env.ENTRA_REDIRECT_URI = "https://app.example.test/auth";

    expect(getAuthConfig()).toEqual({
      provider: "entra",
      clientId: "client",
      tenantId: "tenant",
      apiScope: "api://client/access",
      redirectUri: "https://app.example.test/auth",
    });
  });
});
