import pg from "pg";
import { timingSafeEqual } from "node:crypto";
import { privateResponse } from "./admission.mjs";
import { credentialsFor, getBalance } from "./free-venues.mjs";

const COOKIE = "__Host-cauche-free";
const SECRET = /^[A-Za-z0-9_-]{43}$/;
const HASH = /^[0-9a-f]{64}$/;
const SESSION_TTL = 43200;
const MAX_BODY = 20000;
const text = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

class HttpError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
  }
}

function requireCondition(ok, status, code) {
  if (!ok) throw new HttpError(status, code);
}

function json(data, status = 200, headers = {}) {
  return Response.json(data, { status, headers });
}

function randomSecret() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function hash(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", text.encode(value)));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
}

function secureEqual(left, right) {
  return left.length === right.length && timingSafeEqual(text.encode(left), text.encode(right));
}

function cookie(token, maxAge) {
  return COOKIE + "=" + token + "; Path=/; Max-Age=" + maxAge + "; Secure; HttpOnly; SameSite=Strict";
}

function sessionToken(request) {
  const cookies = (request.headers.get("Cookie") ?? "").split(";").map(value => value.trim());
  const tokens = cookies.filter(value => value.startsWith(COOKIE + "="));
  const token = tokens[0]?.slice(COOKIE.length + 1);
  return tokens.length === 1 && SECRET.test(token) ? token : null;
}

function publicSession(env, csrf_token) {
  return { email: env.CAUCHE_OWNER_EMAIL, role: "admin", csrf_token };
}

export async function readJson(request) {
  requireCondition(request.headers.get("Content-Type")?.split(";")[0] === "application/json",
    415, "JSON_REQUIRED");
  requireCondition(request.body && Number(request.headers.get("Content-Length") ?? 0) <= MAX_BODY,
    413, "REQUEST_TOO_LARGE");
  const reader = request.body.getReader();
  const body = new Uint8Array(MAX_BODY);
  let length = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      if (length + value.length > MAX_BODY) {
        await reader.cancel();
        throw new HttpError(413, "REQUEST_TOO_LARGE");
      }
      body.set(value, length);
      length += value.length;
    }
    const value = JSON.parse(decoder.decode(body.subarray(0, length)));
    requireCondition(value && typeof value === "object" && !Array.isArray(value), 400, "INVALID_REQUEST");
    return value;
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, "INVALID_JSON");
  } finally {
    reader.releaseLock();
  }
}

async function throttle(binding, key) {
  const result = await binding.limit({ key });
  requireCondition(result.success, 429, "RATE_LIMITED");
}

async function validateLogin(request, env) {
  await throttle(env.LOGIN_LIMIT, request.headers.get("CF-Connecting-IP") ?? "unknown");
  const { email, password } = await readJson(request);
  requireCondition(typeof email === "string" && email.length <= 254 && typeof password === "string"
    && SECRET.test(password), 401, "INVALID_CREDENTIALS");
  const passwordHash = await hash(password);
  requireCondition(secureEqual(passwordHash, env.CAUCHE_OWNER_PASSWORD_HASH)
    && email.trim().toLowerCase() === env.CAUCHE_OWNER_EMAIL.toLowerCase(), 401, "INVALID_CREDENTIALS");
}

async function login(env, db) {
  const token = randomSecret();
  const csrf = randomSecret();
  await db.query("DELETE FROM cauche_free.sessions WHERE expires_at <= now()");
  await db.query(
    "INSERT INTO cauche_free.sessions (token_hash, csrf_token, expires_at) VALUES ($1, $2, now() + interval '12 hours')",
    [await hash(token), csrf],
  );
  return json(publicSession(env, csrf), 200, { "Set-Cookie": cookie(token, SESSION_TTL) });
}

async function activeSession(request, db) {
  const token = sessionToken(request);
  requireCondition(token !== null, 401, "NOT_AUTHENTICATED");
  const tokenHash = await hash(token);
  const { rows } = await db.query(
    "SELECT csrf_token FROM cauche_free.sessions WHERE token_hash = $1 AND expires_at > now()", [tokenHash],
  );
  requireCondition(rows.length === 1, 401, "NOT_AUTHENTICATED");
  if (request.method !== "GET") {
    requireCondition(secureEqual(request.headers.get("X-CSRF-Token") ?? "", rows[0].csrf_token), 403, "CSRF");
  }
  return { tokenHash, csrf: rows[0].csrf_token };
}

async function keyFor(env) {
  const bytes = Uint8Array.from(atob(env.CAUCHE_FREE_KEY), char => char.charCodeAt(0));
  requireCondition(bytes.length === 32, 503, "SERVICE_UNAVAILABLE");
  return crypto.subtle.importKey("raw", bytes, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function encrypt(env, aad, value) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: text.encode(aad) },
    await keyFor(env), text.encode(JSON.stringify(value)));
  const bytes = new Uint8Array(iv.length + ciphertext.byteLength);
  bytes.set(iv);
  bytes.set(new Uint8Array(ciphertext), iv.length);
  return btoa(String.fromCharCode(...bytes));
}

async function decrypt(env, aad, encoded) {
  const bytes = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
  const plaintext = await crypto.subtle.decrypt({
    name: "AES-GCM", iv: bytes.subarray(0, 12), additionalData: text.encode(aad),
  }, await keyFor(env), bytes.subarray(12));
  return JSON.parse(decoder.decode(plaintext));
}

function publicConnection(row) {
  const real = row.environment === "REAL_READ_ONLY";
  return {
    environment: row.environment,
    exchange: row.exchange,
    enabled: row.enabled,
    configured: row.configured ?? Boolean(row.ciphertext),
    status: row.status,
    last_sync: row.verified_at,
    capabilities: row.currency ? { currency: row.currency, ...(real ? {} : { demo_balance: row.balance }) } : {},
    ...(real ? { balance: row.balance } : {}),
  };
}

const PUBLIC_COLUMNS = "environment, exchange, enabled, status, currency, balance, verified_at, ciphertext IS NOT NULL AS configured";

async function connection(request, url, env, db, match, venueReader) {
  const environment = match[1] === "real-connections" ? "REAL_READ_ONLY" : "BROKER_DEMO";
  const exchange = match[2];
  const testing = Boolean(match[3]);
  if (!exchange && request.method === "GET") {
    const { rows } = await db.query(
      "SELECT " + PUBLIC_COLUMNS + " FROM cauche_free.connections WHERE environment = $1 ORDER BY exchange",
      [environment],
    );
    return json({ items: rows.map(publicConnection) });
  }

  requireCondition(exchange && (testing ? request.method === "POST" : ["PUT", "DELETE"].includes(request.method)),
    405, "METHOD_NOT_ALLOWED");
  const payload = request.method === "DELETE" ? { confirm: url.searchParams.get("confirm") === "true" }
    : await readJson(request);
  requireCondition(payload.confirm === true, 400, "CONFIRM_REQUIRED");
  const scope = [environment, exchange];
  const aad = scope.join(":");

  if (testing) {
    await throttle(env.VENUE_LIMIT, aad);
    const { rows: [current] } = await db.query(
      "SELECT enabled, ciphertext, revision FROM cauche_free.connections WHERE environment = $1 AND exchange = $2",
      scope,
    );
    requireCondition(current?.enabled && current.ciphertext, 409, "NOT_CONFIGURED");
    const credentials = await decrypt(env, aad, current.ciphertext);
    let verified;
    try {
      verified = await venueReader(exchange, environment, credentials);
    } catch {
      verified = null;
    }
    const { rows: [updated] } = await db.query(
      "UPDATE cauche_free.connections SET status = $3, currency = $4, balance = $5, verified_at = now(), "
      + "revision = revision + 1 WHERE environment = $1 AND exchange = $2 AND revision = $6 RETURNING " + PUBLIC_COLUMNS,
      [...scope, verified ? "VERIFIED" : "ERROR", verified?.currency ?? null, verified?.balance ?? null, current.revision],
    );
    requireCondition(updated, 409, "CONNECTION_CHANGED");
    requireCondition(verified, 422, "ACCOUNT_NOT_VERIFIED");
    return json(publicConnection(updated));
  }

  let ciphertext = null;
  let enabled = false;
  let keep = false;
  if (request.method === "PUT") {
    requireCondition(typeof payload.enabled === "boolean", 422, "INVALID_REQUEST");
    enabled = payload.enabled;
    keep = payload.credentials && typeof payload.credentials === "object" && !Array.isArray(payload.credentials)
      && Object.keys(payload.credentials).length === 0;
    if (!keep) {
      const credentials = credentialsFor(exchange, environment, payload.credentials);
      requireCondition(credentials, 422, "INVALID_CREDENTIALS");
      ciphertext = await encrypt(env, aad, credentials);
    }
  }

  const { rows: [updated] } = await db.query(
    "UPDATE cauche_free.connections SET ciphertext = CASE WHEN $4 THEN ciphertext ELSE $3::text END, enabled = $5, "
    + "status = CASE WHEN (CASE WHEN $4 THEN ciphertext ELSE $3::text END) IS NULL THEN 'NOT_CONFIGURED' "
    + "WHEN $5 THEN 'UNVERIFIED' ELSE 'DISABLED' END, currency = NULL, balance = NULL, verified_at = NULL, "
    + "revision = revision + 1 WHERE environment = $1 AND exchange = $2 RETURNING " + PUBLIC_COLUMNS,
    [...scope, ciphertext, Boolean(keep), enabled],
  );
  requireCondition(updated, 503, "SERVICE_UNAVAILABLE");
  return json(publicConnection(updated));
}

function configured(env, url) {
  return /^https:\/\/cauche-pulse\.[a-z0-9-]+\.workers\.dev$/.test(env.APP_ORIGIN ?? "")
    && url.origin === env.APP_ORIGIN && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(env.CAUCHE_OWNER_EMAIL ?? "")
    && HASH.test(env.CAUCHE_OWNER_PASSWORD_HASH ?? "") && /^[A-Za-z0-9+/]{43}=$/.test(env.CAUCHE_FREE_KEY ?? "")
    && Boolean(env.CAUCHE_DB?.connectionString && env.LOGIN_LIMIT && env.VENUE_LIMIT);
}

async function startDb(env) {
  const db = new pg.Client({
    connectionString: env.CAUCHE_DB.connectionString,
    connectionTimeoutMillis: 5000,
    query_timeout: 8000,
  });
  await db.connect();
  return db;
}

export function makeWorker({ connect = startDb, venueReader = getBalance } = {}) {
  return {
    async fetch(request, env, ctx) {
      const url = new URL(request.url);
      let db;
      let response;
      try {
        requireCondition(configured(env, url), 503, "SERVICE_UNAVAILABLE");
        if (url.pathname !== "/api" && !url.pathname.startsWith("/api/")) {
          requireCondition(["GET", "HEAD"].includes(request.method), 405, "METHOD_NOT_ALLOWED");
          const asset = privateResponse(await env.ASSETS.fetch(request), url.origin);
          asset.headers.set("X-Robots-Tag", "noindex, nofollow");
          return asset;
        }
        const writable = ["POST", "PUT", "DELETE"].includes(request.method);
        requireCondition(request.method === "GET" || writable, 405, "METHOD_NOT_ALLOWED");
        if (writable) requireCondition(request.headers.get("Origin") === url.origin, 403, "ORIGIN");

        const path = url.pathname;
        if (path === "/api/auth/login" && request.method === "POST") {
          await validateLogin(request, env);
          db = await connect(env);
          response = await login(env, db);
        } else {
          requireCondition(sessionToken(request) !== null, 401, "NOT_AUTHENTICATED");
          db = await connect(env);
          const { tokenHash, csrf } = await activeSession(request, db);
          const match = /^\/api\/admin\/(connections|real-connections)(?:\/(DERIV|KALSHI)(\/test)?)?$/.exec(path);
          if (path === "/api/auth/session" && request.method === "GET") {
            response = json(publicSession(env, csrf));
          } else if (path === "/api/auth/logout" && request.method === "POST") {
            await db.query("DELETE FROM cauche_free.sessions WHERE token_hash = $1", [tokenHash]);
            response = new Response(null, { status: 204, headers: { "Set-Cookie": cookie("", 0) } });
          } else if (match) {
            response = await connection(request, url, env, db, match, venueReader);
          } else {
            response = json({ detail: "NOT_FOUND" }, 404);
          }
        }
      } catch (error) {
        response = error instanceof HttpError
          ? json({ detail: error.message }, error.status)
          : json({ detail: "SERVICE_UNAVAILABLE" }, 503);
      } finally {
        if (db) ctx.waitUntil(db.end().catch(() => undefined));
      }
      response.headers.set("X-Robots-Tag", "noindex, nofollow");
      return privateResponse(response, url.origin);
    },
  };
}

export default makeWorker();
