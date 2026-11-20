import { createPrivateKey } from "node:crypto";

const DERIV_URL = "https://api.derivws.com/trading/v1/options/accounts";
const BALANCE_PATH = "/trade-api/v2/portfolio/balance";
const KALSHI_DEMO = "https://external-api.demo.kalshi.co";
const KALSHI_REAL = "https://external-api.kalshi.com";
const text = new TextEncoder();
const ACCOUNT_ID = /^[A-Za-z0-9._:\-]{1,100}$/;
const CURRENCY = /^[A-Z0-9]{2,20}$/;
const DECIMAL = /^\d{1,24}(\.\d{1,18})?$/;

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
  if (typeof key_id !== "string" || !/^[A-Za-z0-9_\-]{1,100}$/.test(key_id)) return null;
  if (typeof private_key !== "string" || private_key.length > 12000) return null;
  if (!/^-----BEGIN (RSA )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]+-----END (RSA )?PRIVATE KEY-----\s*$/.test(private_key)) return null;
  return { key_id, private_key };
}

async function readBalance(url, headers, fetcher) {
  const response = await fetcher(url, {
    method: "GET", headers: { ...headers, Accept: "application/json" },
    redirect: "manual", signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error("VENUE_REJECTED");
  return response.json();
}

export async function getBalance(exchange, environment, credentials, fetcher = fetch) {
  const real = environment === "REAL_READ_ONLY";
  if (exchange === "DERIV") {
    const { data } = await readBalance(DERIV_URL, {
      "Deriv-App-ID": credentials.app_id, Authorization: "Bearer " + credentials.token,
    }, fetcher);
    if (!Array.isArray(data)) throw new Error("INVALID_RESPONSE");
    const matches = data.filter(row => row?.account_type === (real ? "real" : "demo")
      && row.status === "active" && (!credentials.account_id || row.account_id === credentials.account_id));
    const balance = String(matches[0]?.balance ?? "");
    if (matches.length !== 1 || !CURRENCY.test(matches[0]?.currency) || !DECIMAL.test(balance)) {
      throw new Error("ACCOUNT_NOT_VERIFIED");
    }
    return { balance, currency: matches[0].currency };
  }
  const key = createPrivateKey(credentials.private_key);
  if (key.asymmetricKeyType !== "rsa" || key.asymmetricKeyDetails.modulusLength < 2048) {
    throw new Error("INVALID_KEY");
  }
  const signingKey = await crypto.subtle.importKey(
    "pkcs8", key.export({ format: "der", type: "pkcs8" }),
    { name: "RSA-PSS", hash: "SHA-256" }, false, ["sign"],
  );
  const timestamp = String(Date.now());
  const signature = new Uint8Array(await crypto.subtle.sign(
    { name: "RSA-PSS", saltLength: 32 }, signingKey, text.encode(timestamp + "GET" + BALANCE_PATH),
  ));
  const payload = await readBalance((real ? KALSHI_REAL : KALSHI_DEMO) + BALANCE_PATH, {
    "KALSHI-ACCESS-KEY": credentials.key_id,
    "KALSHI-ACCESS-TIMESTAMP": timestamp,
    "KALSHI-ACCESS-SIGNATURE": btoa(String.fromCharCode(...signature)),
  }, fetcher);
  const { balance_dollars, balance } = payload ?? {};
  if (balance_dollars != null && DECIMAL.test(String(balance_dollars))) {
    return { balance: String(balance_dollars), currency: "USD" };
  }
  if (!Number.isSafeInteger(balance) || balance < 0) throw new Error("INVALID_RESPONSE");
  return { balance: (balance / 100).toFixed(2), currency: "USD" };
}
