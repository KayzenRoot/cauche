import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, constants, verify } from "node:crypto";
import test from "node:test";
import { makeWorker } from "../src/free-worker.mjs";
import { credentialsFor, getBalance } from "../src/free-venues.mjs";

const origin = "https://cauche-pulse.test-owner.workers.dev";
const password = "a".repeat(43);
const email = "owner@example.test";
const env = {
  APP_ORIGIN: origin,
  CAUCHE_OWNER_EMAIL: email,
  CAUCHE_OWNER_PASSWORD_HASH: createHash("sha256").update(password).digest("hex"),
  CAUCHE_FREE_KEY: Buffer.alloc(32).toString("base64"),
  CAUCHE_DB: { connectionString: "local-fixture" },
  LOGIN_LIMIT: { async limit() { return { success: true }; } },
  VENUE_LIMIT: { async limit() { return { success: true }; } },
};
const ctx = { waitUntil() {} };
const sessionCookie = "__Host-cauche-free=" + "b".repeat(43);

function incoming(path, { method = "GET", body, headers } = {}) {
  return new Request(origin + path, {
    method,
    headers: { Origin: origin, ...(body ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
}

test("rejects anonymous reads, cross-origin writes, and invalid logins without database access", async () => {
  let connects = 0;
  const worker = makeWorker({ async connect() { connects++; throw new Error("UNEXPECTED"); } });
  assert.equal((await worker.fetch(incoming("/api/admin/connections"), env, ctx)).status, 401);
  const login = { method: "POST", body: { email, password } };
  assert.equal((await worker.fetch(incoming("/api/auth/login", {
    ...login, headers: { Origin: "https://example.invalid" },
  }), env, ctx)).status, 403);
  assert.equal((await worker.fetch(incoming("/api/auth/login", {
    ...login, body: { email, password: "wrong" },
  }), env, ctx)).status, 401);
  assert.equal(connects, 0);
});

test("logs in with a revocable secure session and stores only the token hash", async () => {
  const calls = [];
  const worker = makeWorker({ async connect() {
    return {
      async query(...args) { calls.push(args); return { rows: [] }; },
      async end() {},
    };
  } });
  const response = await worker.fetch(incoming("/api/auth/login", { method: "POST", body: { email, password } }), env, ctx);
  assert.equal(response.status, 200);
  const session = await response.json();
  assert.equal(session.email, email);
  const cookie = response.headers.get("Set-Cookie");
  assert.match(cookie, /; Secure; HttpOnly; SameSite=Strict$/);
  const token = /^__Host-cauche-free=([A-Za-z0-9_-]{43});/.exec(cookie)?.[1];
  assert.ok(token);
  assert.equal(calls[1][1][0], createHash("sha256").update(token).digest("hex"));
  assert.equal(calls[1][1][1], session.csrf_token);
  assert.equal(response.headers.get("Cache-Control"), "private, no-store");
});

test("blocks authenticated writes without CSRF and exposes no execution endpoints", async () => {
  const csrf = "csrf-fixture";
  let queries = 0;
  const worker = makeWorker({ async connect() {
    return {
      async query(sql) { queries++; assert.match(sql, /SELECT csrf_token/); return { rows: [{ csrf_token: csrf }] }; },
      async end() {},
    };
  } });
  const call = headers => worker.fetch(incoming("/api/bot/start", { method: "POST", body: {}, headers }), env, ctx);
  assert.equal((await call({ Cookie: sessionCookie })).status, 403);
  assert.equal((await call({ Cookie: sessionCookie, "X-CSRF-Token": csrf })).status, 404);
  assert.equal(queries, 2);
});

test("keeps Deriv demo and real selectors isolated on the fixed GET route", async () => {
  const fetcher = async (url, request) => {
    assert.equal(url, "https://api.derivws.com/trading/v1/options/accounts");
    assert.equal(request.method, "GET");
    assert.equal(request.redirect, "error");
    return Response.json({ data: [
      { account_type: "demo", status: "active", account_id: "demo-fixture", currency: "USD", balance: "12.40" },
      { account_type: "real", status: "active", account_id: "real-fixture", currency: "USD", balance: "0.30" },
    ] });
  };
  const base = { app_id: "app", token: "synthetic" };
  assert.equal(credentialsFor("DERIV", "REAL_READ_ONLY", base), null);
  assert.deepEqual(await getBalance("DERIV", "BROKER_DEMO", base, fetcher), { balance: "12.40", currency: "USD" });
  await assert.rejects(getBalance("DERIV", "REAL_READ_ONLY", { ...base, account_id: "demo-fixture" }, fetcher));
  assert.deepEqual(await getBalance("DERIV", "REAL_READ_ONLY", {
    ...base, account_id: "real-fixture",
  }, fetcher), { balance: "0.30", currency: "USD" });
});

test("uses the matching Kalshi host and signs only the canonical balance GET", async () => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const pem = privateKey.export({ type: "pkcs1", format: "pem" });
  for (const [environment, host] of [
    ["BROKER_DEMO", "external-api.demo.kalshi.co"], ["REAL_READ_ONLY", "external-api.kalshi.com"],
  ]) {
    const result = await getBalance("KALSHI", environment, { key_id: "key-test", private_key: pem },
      async (url, request) => {
        const path = "/trade-api/v2/portfolio/balance";
        assert.equal(url, "https://" + host + path);
        assert.equal(request.method, "GET");
        assert.equal(request.redirect, "error");
        const timestamp = request.headers["KALSHI-ACCESS-TIMESTAMP"];
        const signature = Buffer.from(request.headers["KALSHI-ACCESS-SIGNATURE"], "base64");
        const message = Buffer.from(timestamp + "GET" + path);
        assert.equal(verify("sha256", message, {
          key: publicKey, padding: constants.RSA_PKCS1_PSS_PADDING, saltLength: 32,
        }, signature), true);
        return Response.json({ balance: 1240 });
      });
    assert.deepEqual(result, { balance: "12.40", currency: "USD" });
  }
});
