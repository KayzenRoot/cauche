import { ResearchError, responseJson } from "./free-research.mjs";

const KALSHI = "https://external-api.kalshi.com/trade-api/v2/markets/";
const TICKER = /^[A-Z0-9][A-Z0-9._-]{0,99}$/;
const PRICE = /^(?:0(?:\.\d{1,4})?|1(?:\.0{1,4})?)$/;
const QUANTITY = /^\d{1,10}(?:\.\d{1,2})?$/;

function levels(raw) {
  if (!Array.isArray(raw) || raw.length > 5) throw new Error("Invalid book levels");
  return raw.map(level => {
    if (!Array.isArray(level) || level.length !== 2 || typeof level[0] !== "string"
      || typeof level[1] !== "string" || !PRICE.test(level[0]) || !QUANTITY.test(level[1])) {
      throw new Error("Invalid book level");
    }
    return level;
  }).sort((left, right) => Number(right[0]) - Number(left[0]));
}

function ask(oppositeBid) {
  if (!oppositeBid) return null;
  return {
    price: ((10_000 - Math.round(Number(oppositeBid[0]) * 10_000)) / 10_000).toFixed(4),
    quantity: oppositeBid[1],
  };
}

export async function captureBook(ticker, fetcher = fetch) {
  if (!TICKER.test(ticker)) throw new ResearchError(422, "INVALID_EVIDENCE");
  const url = KALSHI + ticker + "/orderbook?depth=5";
  try {
    const response = await fetcher(url, {
      method: "GET", headers: { Accept: "application/json" },
      redirect: "manual", signal: AbortSignal.timeout(7000),
    });
    if (!response.ok || Number(response.headers.get("Content-Length") ?? 0) > 4096) {
      throw new Error("Book unavailable");
    }
    const payload = (await responseJson(response, 4096))?.orderbook_fp;
    const yes = levels(payload?.yes_dollars);
    const no = levels(payload?.no_dollars);
    return {
      status: "OBSERVED", source: url, observed_at: new Date().toISOString(),
      yes_bids: yes, no_bids: no, yes_ask: ask(no[0]), no_ask: ask(yes[0]),
    };
  } catch {
    return { status: "UNAVAILABLE", source: url };
  }
}

export async function recordInspection(db, result, book) {
  const official = result.sources[0]?.url;
  const evidence = {
    source: official,
    market_observed_at: result.observed_at,
    event: result.market?.event_ticker ?? null,
    market_status: result.market?.status ?? null,
    outcome: result.market?.result ?? null,
    settled_at: result.market?.settled_at ?? null,
    closes_at: result.rules.closes_at,
    book,
    rules: result.rules,
    sources: result.sources,
    model: result.model,
    extraction: result.extraction,
  };
  const { rows } = await db.query(
    "INSERT INTO cauche_free.research_observations (ticker, observed_at, evidence) VALUES ($1, $2, $3) RETURNING id",
    [result.ticker, book.observed_at ?? result.observed_at, JSON.stringify(evidence)],
  );
  return rows[0].id;
}

function integerParameter(params, name, fallback, lower, upper) {
  const raw = params.get(name);
  const value = raw === null ? fallback : /^\d{1,6}$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isInteger(value) || value < lower || value > upper) {
    throw new ResearchError(422, "INVALID_EVIDENCE");
  }
  return value;
}

export async function journalPage(db, params) {
  const limit = integerParameter(params, "limit", 25, 1, 100);
  const offset = integerParameter(params, "offset", 0, 0, 100000);
  const { rows } = await db.query(
    "SELECT id, ticker, observed_at, evidence FROM cauche_free.research_observations ORDER BY id DESC LIMIT $1 OFFSET $2",
    [limit, offset],
  );
  const total = Number((await db.query("SELECT count(*) AS n FROM cauche_free.research_observations")).rows[0].n);
  return {
    total, offset, limit,
    items: rows.map(row => ({
      id: String(row.id), exchange: "KALSHI", instrument: row.ticker,
      timestamp: row.observed_at, reason: "REVIEW_REQUIRED", mode: "READ_ONLY",
      data: { evidence: row.evidence },
    })),
  };
}
