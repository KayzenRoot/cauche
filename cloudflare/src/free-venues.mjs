import { Buffer } from "node:buffer";
import { createPrivateKey, sign } from "node:crypto";
import { responseJson } from "./free-research.mjs";

const DERIV_URL = "https://api.derivws.com/trading/v1/options/accounts";
const BALANCE_PATH = "/trade-api/v2/portfolio/balance";
const KALSHI_DEMO = "https://external-api.demo.kalshi.co";
const KALSHI_REAL = "https://external-api.kalshi.com";
const ACCOUNT_ID = /^[A-Za-z0-9._:-]{1,100}$/;
const CURRENCY = /^[A-Z0-9]{2,20}$/;
const DECIMAL = /^\d{1,24}(\.\d{1,18})?$/;

export class VenueVerificationError extends Error {
  constructor(code, status = 422) {
    super(code);
    this.status = status;
  }
}

export function credentialsFor(exchange, environment, value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const { app_id, token, account_id, key_id, private_key } = value;
  if (exchange === "DERIV") {
    if (typeof app_id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(app_id)) return null;
    if (typeof token !== "string" || !/^[\x21-\x7e]{1,4096}$/.test(token)) return null;
    if (account_id !== undefined && (typeof account_id !== "string" || !ACCOUNT_ID.test(account_id))) return null;
    if (environment === "REAL_READ_ONLY" && !account_id) return null;
    return { app_id, token, account_id };
  }
  if (typeof key_id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(key_id)) return null;
  if (typeof private_key !== "string" || private_key.length > 12000) return null;
  if (!/^-----BEGIN ((?:RSA )?PRIVATE KEY)-----\s+[A-Za-z0-9+/=\s]+-----END \1-----\s*$/.test(private_key)) return null;
  return { key_id, private_key };
}

function rejection(exchange, status, payload) {
  if (status === 429 || status >= 500) return new VenueVerificationError("VENUE_UNAVAILABLE", 503);
  if (exchange === "DERIV") {
    if (status === 401 || status === 400) {
      const messages = Array.isArray(payload?.errors) ? payload.errors.map(error => error?.message) : [];
      if (messages.some(message => [
        "Invalid App ID",
        "App ID not recognised",
        "Deriv-App-ID header is required for PAT tokens",
      ].includes(message))) return new VenueVerificationError("DERIV_APP_ID_REJECTED");
      if (messages.some(message => ["Expired Access Token", "Token no longer valid"].includes(message))) {
        return new VenueVerificationError("DERIV_TOKEN_REJECTED");
      }
    }
    if (status === 401) return new VenueVerificationError("DERIV_AUTH_REJECTED");
    if (status === 403) return new VenueVerificationError("DERIV_ACCESS_DENIED");
    if (status === 404 && Array.isArray(payload?.errors) && payload.errors.some(error => error?.code === "AccountNotFound")) {
      return new VenueVerificationError("DERIV_ACCOUNT_NOT_FOUND");
    }
  } else {
    if (status === 401) {
      const error = payload?.error ?? payload;
      if (error?.code === "header_timestamp_expired") return new VenueVerificationError("KALSHI_CLOCK_SKEW", 503);
      if (error?.code === "authentication_error") {
        if (["INVALID_PARAMETER", "NOT_FOUND"].includes(error.details)) {
          return new VenueVerificationError("KALSHI_KEY_ID_REJECTED");
        }
        if (error.details === "INCORRECT_API_KEY_SIGNATURE") {
          return new VenueVerificationError("KALSHI_KEY_SIGNATURE_REJECTED");
        }
        if (error.details === "PERMISSION_DENIED") return new VenueVerificationError("KALSHI_ACCESS_DENIED");
      }
      return new VenueVerificationError("KALSHI_AUTH_REJECTED");
    }
    if (status === 403) return new VenueVerificationError("KALSHI_SCOPE_REQUIRED");
  }
  return new VenueVerificationError("VENUE_RESPONSE_INVALID", 502);
}

async function readBalance(exchange, url, headers, fetcher) {
  let response;
  try {
    response = await fetcher(url, {
      method: "GET", headers: { ...headers, Accept: "application/json" },
      redirect: "manual", signal: AbortSignal.timeout(8000),
    });
  } catch {
    throw new VenueVerificationError("VENUE_UNAVAILABLE", 503);
  }
  const payload = await responseJson(response, 65536).catch(() => null);
  if (!response.ok) throw rejection(exchange, response.status, payload);
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new VenueVerificationError("VENUE_RESPONSE_INVALID", 502);
  }
  return payload;
}

function derivBalance(data, environment, accountId) {
  if (!Array.isArray(data)) throw new VenueVerificationError("VENUE_RESPONSE_INVALID", 502);
  const type = environment === "REAL_READ_ONLY" ? "real" : "demo";
  const selected = data.filter(row => accountId ? row?.account_id === accountId : row?.account_type === type);
  if (selected.length === 0) {
    throw new VenueVerificationError(accountId ? "DERIV_ACCOUNT_NOT_FOUND" : "DERIV_ENV_ACCOUNT_UNAVAILABLE");
  }
  const eligible = selected.filter(row => row.account_type === type);
  if (eligible.length === 0) throw new VenueVerificationError("DERIV_ACCOUNT_ENV_MISMATCH");
  const active = eligible.filter(row => row.status === "active");
  if (active.length === 0) throw new VenueVerificationError("DERIV_ACCOUNT_INACTIVE");
  if (active.length !== 1 || (accountId && selected.length !== 1)) {
    throw new VenueVerificationError("DERIV_ACCOUNT_SELECTION_REQUIRED");
  }
  const { balance, currency } = active[0];
  if (!CURRENCY.test(currency) || !DECIMAL.test(String(balance ?? ""))) {
    throw new VenueVerificationError("VENUE_RESPONSE_INVALID", 502);
  }
  return { balance: String(balance), currency };
}

async function kalshiHeaders({ key_id, private_key }) {
  try {
    const key = createPrivateKey(private_key);
    const rsa = key.asymmetricKeyType === "rsa" && key.asymmetricKeyDetails?.modulusLength >= 2048;
    if (!rsa && key.asymmetricKeyType !== "ed25519") throw new VenueVerificationError("KALSHI_PRIVATE_KEY_INVALID");
    const signingKey = rsa ? await crypto.subtle.importKey(
      "pkcs8", key.export({ format: "der", type: "pkcs8" }),
      { name: "RSA-PSS", hash: "SHA-256" }, false, ["sign"],
    ) : null;
    const timestamp = String(Date.now());
    const message = Buffer.from(timestamp + "GET" + BALANCE_PATH, "utf8");
    const signature = signingKey
      ? Buffer.from(await crypto.subtle.sign({ name: "RSA-PSS", saltLength: 32 }, signingKey, message))
      : sign(null, message, key);
    return {
      "KALSHI-ACCESS-KEY": key_id,
      "KALSHI-ACCESS-TIMESTAMP": timestamp,
      "KALSHI-ACCESS-SIGNATURE": signature.toString("base64"),
    };
  } catch {
    throw new VenueVerificationError("KALSHI_PRIVATE_KEY_INVALID");
  }
}

export async function getBalance(exchange, environment, credentials, fetcher = fetch) {
  if (exchange === "DERIV") {
    const { data } = await readBalance(exchange, DERIV_URL, {
      "Deriv-App-ID": credentials.app_id, Authorization: "Bearer " + credentials.token,
    }, fetcher);
    return derivBalance(data, environment, credentials.account_id);
  }
  const root = environment === "REAL_READ_ONLY" ? KALSHI_REAL : KALSHI_DEMO;
  const { balance_dollars, balance } = await readBalance(exchange, root + BALANCE_PATH,
    await kalshiHeaders(credentials), fetcher);
  if (balance_dollars != null && DECIMAL.test(String(balance_dollars))) {
    return { balance: String(balance_dollars), currency: "USD" };
  }
  if (!Number.isSafeInteger(balance) || balance < 0) throw new VenueVerificationError("VENUE_RESPONSE_INVALID", 502);
  return { balance: (balance / 100).toFixed(2), currency: "USD" };
}
