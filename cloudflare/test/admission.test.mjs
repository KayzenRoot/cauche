import assert from "node:assert/strict";
import test from "node:test";
import { authorize, containerRequest, privateResponse } from "../src/admission.mjs";

const env = {
  APP_ORIGIN: "https://cauche-pulse.sample.workers.dev",
  CF_ACCESS_AUD: "a".repeat(64),
  CAUCHE_OWNER_EMAIL: "owner@example.test",
};

function access(email = env.CAUCHE_OWNER_EMAIL, aud = env.CF_ACCESS_AUD) {
  return {aud, getIdentity: async () => ({email})};
}

test("allows only verified owner identity scoped to the configured hostname and audience", async () => {
  const request = new Request(env.APP_ORIGIN);
  assert.equal(await authorize(request, env, access()), null);
  assert.equal((await authorize(request, env, access("someone@example.test"))).status, 403);
  assert.equal((await authorize(request, env, access(undefined, "b".repeat(64)))).status, 403);
  assert.equal((await authorize(new Request("https://preview.cauche-pulse.sample.workers.dev"), env, access())).status, 403);
});

test("fails closed for missing configuration and rejects caller-provided claims", async () => {
  const request = new Request(env.APP_ORIGIN, {
    headers: {"Cf-Access-Jwt-Assertion": "synthetic", "Cf-Access-Authenticated-User-Email": env.CAUCHE_OWNER_EMAIL},
  });
  assert.equal((await authorize(request, {}, access())).status, 503);
  assert.equal((await authorize(request, env, undefined)).status, 403);
  const inaccessible = {...access(), getIdentity: async () => { throw new Error("Identity lost"); }};
  assert.equal((await authorize(request, env, inaccessible)).status, 403);
});

test("forwards only the app cookie while preserving same-origin and upgrade headers", () => {
  const request = new Request(env.APP_ORIGIN + "/api/ws?after=4", {
    headers: {
      Origin: env.APP_ORIGIN, Upgrade: "websocket", Connection: "Upgrade",
      Cookie: "CF_Authorization=synthetic; __Host-cauche=test-local-session",
      "Cf-Access-Jwt-Assertion": "synthetic", Authorization: "Bearer synthetic",
    },
  });
  const forwarded = containerRequest(request);
  assert.equal(forwarded.url, "http://container/api/ws?after=4");
  assert.equal(forwarded.headers.get("Origin"), env.APP_ORIGIN);
  assert.equal(forwarded.headers.get("Upgrade"), "websocket");
  assert.equal(forwarded.headers.get("Cookie"), "__Host-cauche=test-local-session");
  assert.equal(forwarded.headers.get("Authorization"), null);
  assert.equal(forwarded.headers.get("Cf-Access-Jwt-Assertion"), null);
});

test("HTTP responses are private and permit only same-origin WebSocket connections", () => {
  const response = privateResponse(new Response("body"), env.APP_ORIGIN);
  assert.equal(response.headers.get("Cache-Control"), "private, no-store");
  assert.ok(response.headers.get("Content-Security-Policy").includes("wss://cauche-pulse.sample.workers.dev"));
});
