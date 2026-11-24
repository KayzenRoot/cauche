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
  assert.equal((await call({ Cookie: sessionCookie, "X-CSRF-Token": "é" + csrf.slice(1) })).status, 403);
  assert.equal((await call({ Cookie: sessionCookie, "X-CSRF-Token": csrf })).status, 404);
  assert.equal(queries, 3);
});

test("keeps Deriv demo and real selectors isolated on the fixed GET route", async () => {
  const fetcher = async (url, request) => {
    assert.equal(url, "https://api.derivws.com/trading/v1/options/accounts");
    assert.equal(request.method, "GET");
    assert.equal(request.redirect, "manual");
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
        assert.equal(request.redirect, "manual");
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


test("authenticates the default Kalshi Ed25519 PEM with the canonical request", async () => {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  const credentials = { key_id: "fixture-key", private_key: privateKey.export({ type: "pkcs8", format: "pem" }) };
  assert.deepEqual(credentialsFor("KALSHI", "BROKER_DEMO", credentials), credentials);
  assert.deepEqual(await getBalance("KALSHI", "BROKER_DEMO", credentials, async (url, request) => {
    const endpoint = new URL(url);
    assert.equal(endpoint.origin, "https://external-api.demo.kalshi.co");
    assert.equal(endpoint.pathname, "/trade-api/v2/portfolio/balance");
    const timestamp = request.headers["KALSHI-ACCESS-TIMESTAMP"];
    const signature = Buffer.from(request.headers["KALSHI-ACCESS-SIGNATURE"], "base64");
    assert.ok(verify(null, Buffer.from(timestamp + "GET" + endpoint.pathname), publicKey, signature));
    return Response.json({ balance_dollars: "12.1234" });
  }), { balance: "12.1234", currency: "USD" });

  await assert.rejects(getBalance("KALSHI", "BROKER_DEMO", { ...credentials, private_key: "invalid" },
    async () => assert.fail("invalid key reached the network")), { message: "KALSHI_PRIVATE_KEY_INVALID", status: 422 });
});

test("diagnoses documented authentication cases and redacts unknown vendor details", async () => {
  const deriv = { app_id: "fixture-app", token: "fixture-pat" };
  const { privateKey } = generateKeyPairSync("ed25519");
  const kalshi = { key_id: "fixture-key", private_key: privateKey.export({ type: "pkcs8", format: "pem" }) };
  const scenarios = [
    ["DERIV", deriv, 401, { errors: [{ message: "Invalid App ID" }] }, "DERIV_APP_ID_REJECTED", 422],
    ["DERIV", deriv, 401, { errors: [{ message: "Expired Access Token" }] }, "DERIV_TOKEN_REJECTED", 422],
    ["DERIV", deriv, 401, { errors: [{ message: "untrusted raw message with fixture-pat" }] }, "DERIV_AUTH_REJECTED", 422],
    ["DERIV", deriv, 403, { errors: [{ message: "untrusted" }] }, "DERIV_ACCESS_DENIED", 422],
    ["KALSHI", kalshi, 401, { error: { code: "authentication_error", details: "NOT_FOUND" } }, "KALSHI_KEY_ID_REJECTED", 422],
    ["KALSHI", kalshi, 401, { error: { code: "authentication_error", details: "INCORRECT_API_KEY_SIGNATURE" } },
      "KALSHI_KEY_SIGNATURE_REJECTED", 422],
    ["KALSHI", kalshi, 503, { error: { code: "authentication_error", details: "NOT_FOUND" } }, "VENUE_UNAVAILABLE", 503],
    ["KALSHI", kalshi, 403, { code: "insufficient_scope:_read::portfolio_balance_required" }, "KALSHI_SCOPE_REQUIRED", 422],
  ];
  for (const [exchange, credentials, status, payload, message, expected] of scenarios) {
    await assert.rejects(getBalance(exchange, "BROKER_DEMO", credentials, async () => Response.json(payload, { status })),
      { message, status: expected });
  }
  await assert.rejects(getBalance("DERIV", "BROKER_DEMO", deriv, async () => { throw new Error("secret"); }),
    { message: "VENUE_UNAVAILABLE", status: 503 });
});

test("distinguishes Deriv account selection and environment mismatch", async () => {
  const credentials = { app_id: "fixture-app", token: "fixture-pat" };
  const row = { account_type: "demo", status: "active", account_id: "demo-1", currency: "USD", balance: "2" };
  const data = [row, { ...row, account_id: "demo-2" },
    { ...row, account_type: "real", account_id: "real-1" }, { ...row, status: "closed", account_id: "demo-3" }];
  for (const [account_id, message] of [
    [undefined, "DERIV_ACCOUNT_SELECTION_REQUIRED"],
    ["demo-3", "DERIV_ACCOUNT_INACTIVE"],
    ["real-1", "DERIV_ACCOUNT_ENV_MISMATCH"],
    ["demo-4", "DERIV_ACCOUNT_NOT_FOUND"],
  ]) {
    await assert.rejects(getBalance("DERIV", "BROKER_DEMO", { ...credentials, account_id },
      async () => Response.json({ data })), { message });
  }
});

test("edits Deriv identifiers with a stored PAT and rejects a concurrent change", async () => {
  const csrf = "csrf-fixture";
  const headers = { Cookie: sessionCookie, "X-CSRF-Token": csrf };
  const path = "/api/admin/connections/DERIV";
  const row = { environment: "BROKER_DEMO", exchange: "DERIV", ciphertext: null, revision: "0", enabled: true };
  let race = false;
  let observed;
  const worker = makeWorker({
    async connect() {
      return {
        async query(sql, args) {
          if (sql.startsWith("SELECT csrf_token")) return { rows: [{ csrf_token: csrf }] };
          assert.deepEqual(args.slice(0, 2), ["BROKER_DEMO", "DERIV"]);
          if (sql.startsWith("SELECT")) return { rows: [{ ...row }] };
          if (sql.includes("SET status =")) {
            assert.equal(args[5], row.revision);
            return { rows: [{ ...row, status: args[2] }] };
          }
          assert.match(sql, /revision = \$6/);
          if (race || (args[5] !== null && args[5] !== row.revision)) return { rows: [] };
          row.ciphertext = args[2];
          row.revision = String(Number(row.revision) + 1);
          return { rows: [{ ...row, status: "UNVERIFIED" }] };
        },
        async end() {},
      };
    },
    async venueReader(exchange, environment, credentials) {
      observed = { exchange, environment, credentials };
      return { balance: "12", currency: "USD" };
    },
  });
  const update = async (credentials, overrides = {}) => worker.fetch(incoming(path, {
    method: "PUT", headers, body: { enabled: true, confirm: true, credentials }, ...overrides,
  }), env, ctx);

  const missing = await update({ app_id: "app-new" });
  assert.equal(missing.status, 422);
  assert.deepEqual(await missing.json(), { detail: "DERIV_TOKEN_REQUIRED" });
  assert.equal((await update({ app_id: "app-original", account_id: "account-old", token: "fixture-pat" })).status, 200);
  const originalCiphertext = row.ciphertext;
  assert.doesNotMatch(originalCiphertext, /fixture-pat/);
  assert.equal((await update({ app_id: "app-new" }, { headers: { Cookie: sessionCookie } })).status, 403);
  assert.equal((await update({ app_id: "app-new" }, { body: { enabled: true, confirm: false, credentials: {} } })).status, 400);
  assert.equal(row.ciphertext, originalCiphertext);
  const updated = await update({ app_id: "app-new", account_id: null });
  assert.equal(updated.status, 200);
  assert.doesNotMatch(await updated.text(), /fixture-pat|app-new|ciphertext/);

  const tested = await worker.fetch(incoming(path + "/test", { method: "POST", headers, body: { confirm: true } }), env, ctx);
  assert.equal(tested.status, 200);
  assert.deepEqual(observed, { exchange: "DERIV", environment: "BROKER_DEMO",
    credentials: { app_id: "app-new", token: "fixture-pat" } });
  race = true;
  const conflict = await update({ app_id: "app-concurrent" });
  assert.equal(conflict.status, 409);
  assert.deepEqual(await conflict.json(), { detail: "CONNECTION_CHANGED" });
});
