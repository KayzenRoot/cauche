import { ResearchError, marketRules } from "./free-research.mjs";

const LIMIT = 3;
const FINAL = new Set(["finalized", "settled"]);
const encoder = new TextEncoder();

export async function contractKey(ticker, market) {
  if (!market?.event_ticker || !market.primary || !market.closes_at) return null;
  const close = Date.parse(market.closes_at);
  if (!Number.isFinite(close)) return null;
  const terms = JSON.stringify([
    ticker, market.event_ticker, new Date(close).toISOString(), market.primary, market.secondary ?? "",
  ]);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(terms)));
  return Array.from(hash, value => value.toString(16).padStart(2, "0")).join("");
}

export function finalResult(market, observedAt) {
  if (!FINAL.has(market.status) || !["yes", "no"].includes(market.result)) return null;
  const settled = Date.parse(market.settled_at);
  if (!Number.isFinite(settled) || !Number.isFinite(Date.parse(observedAt)) || settled > Date.parse(observedAt)
    || settled < Date.UTC(2020, 0, 1)) return null;
  return { result: market.result.toUpperCase(), settled_at: new Date(settled).toISOString() };
}

function storedTerms(market) {
  return JSON.stringify({ event: market.event_ticker, primary: market.primary, secondary: market.secondary ?? "" });
}

async function lookup(ticker) {
  try {
    return await marketRules(ticker);
  } catch (error) {
    if (!(error instanceof ResearchError) || error.status !== 404) throw error;
    return marketRules(ticker, fetch, { historical: true });
  }
}

export async function enqueueSql(db, observation, evidence) {
  const market = { ...observation.rules, ...observation.market };
  const key = await contractKey(observation.ticker, market);
  const stamp = evidence.book?.observed_at ?? observation.observed_at;
  const args = [observation.ticker, stamp, JSON.stringify({ ...evidence, contract_key: key })];
  const insert = "INSERT INTO cauche_free.research_observations (ticker, observed_at, evidence) VALUES ($1, $2, $3) RETURNING id";
  if (!key) return (await db.query(insert, args)).rows[0].id;
  const final = finalResult(market, observation.observed_at);
  const append = "WITH queued AS (INSERT INTO cauche_free.research_settlements "
    + "(ticker, contract_key, due_at, next_check_at, status, result, settled_at, observed_at, source, terms) "
    + "VALUES ($1, $4, $5::timestamptz, $5::timestamptz + interval '15 minutes', $6, $7, $8, $9, $10, $11) "
    + "ON CONFLICT (ticker, contract_key) DO UPDATE SET status = 'SETTLED', result = EXCLUDED.result, "
    + "settled_at = EXCLUDED.settled_at, observed_at = EXCLUDED.observed_at, source = EXCLUDED.source "
    + "WHERE EXCLUDED.status = 'SETTLED' AND cauche_free.research_settlements.status IN ('PENDING','UNAVAILABLE')) ";
  return (await db.query(append + insert, [
    ...args, key, market.closes_at, final ? "SETTLED" : "PENDING", final?.result ?? null,
    final?.settled_at ?? null, final ? observation.observed_at : null,
    final ? observation.sources[0].url : null, storedTerms(market),
  ])).rows[0].id;
}

export const joinPriorSettlement = "(s.due_at = CASE WHEN pg_input_is_valid(j.evidence->>'closes_at', 'timestamptz') "
  + "THEN (j.evidence->>'closes_at')::timestamptz END AND s.terms = jsonb_build_object("
  + "'event', j.evidence->>'event', 'primary', j.evidence->'rules'->>'primary', "
  + "'secondary', COALESCE(j.evidence->'rules'->>'secondary', '')))";

async function queueEarlierObservations(db) {
  const { rows } = await db.query(
    "SELECT DISTINCT j.ticker, j.evidence->>'event' AS event_ticker, j.evidence->>'closes_at' AS closes_at, "
    + "j.evidence->'rules'->>'primary' AS primary, COALESCE(j.evidence->'rules'->>'secondary', '') AS secondary "
    + "FROM cauche_free.research_observations j WHERE NOT (j.evidence ? 'contract_key') "
    + "AND length(j.evidence->>'event') > 0 AND length(j.evidence->'rules'->>'primary') > 0 "
    + "AND pg_input_is_valid(j.evidence->>'closes_at', 'timestamptz') "
    + "AND NOT EXISTS (SELECT 1 FROM cauche_free.research_settlements s WHERE s.ticker = j.ticker AND "
    + joinPriorSettlement + ") ORDER BY j.ticker, closes_at, event_ticker, primary, secondary LIMIT $1",
    [LIMIT],
  );
  for (const row of rows) {
    const key = await contractKey(row.ticker, row);
    if (!key) continue;
    await db.query(
      "INSERT INTO cauche_free.research_settlements (ticker, contract_key, due_at, next_check_at, terms) "
      + "VALUES ($1, $2, $3::timestamptz, $3::timestamptz + interval '15 minutes', $4) ON CONFLICT DO NOTHING",
      [row.ticker, key, row.closes_at, storedTerms(row)],
    );
  }
}

export async function processSettlements(db, readMarket = lookup) {
  await queueEarlierObservations(db);
  const lease = crypto.randomUUID();
  // Reserve a short lease with an atomic, bounded query before any network requests.
  const { rows } = await db.query(
    "WITH due AS (SELECT ticker, contract_key FROM cauche_free.research_settlements "
    + "WHERE status = 'PENDING' AND next_check_at <= now() "
    + "ORDER BY next_check_at, ticker, contract_key LIMIT $1 FOR UPDATE SKIP LOCKED) "
    + "UPDATE cauche_free.research_settlements AS r SET next_check_at = now() + interval '30 minutes', "
    + "attempts = LEAST(r.attempts + 1, 96), lease_id = $2 FROM due WHERE r.ticker = due.ticker AND r.contract_key = due.contract_key "
    + "RETURNING r.ticker, r.contract_key, r.attempts, r.next_check_at",
    [LIMIT, lease],
  );
  const results = await Promise.all(rows.map(async row => {
    let terminal = row.attempts >= 96 ? "UNAVAILABLE" : "PENDING";
    let resolution = null;
    let source = null;
    let observedAt = new Date().toISOString();
    try {
      const market = await readMarket(row.ticker);
      observedAt = new Date().toISOString();
      const key = await contractKey(row.ticker, market);
      if (key && key !== row.contract_key) terminal = "RULES_CHANGED";
      if (key === row.contract_key) {
        resolution = finalResult(market, observedAt);
        if (resolution) terminal = "SETTLED";
        if (resolution) source = market.source;
      }
    } catch (error) {
      if (!(error instanceof ResearchError)) throw error;
      // The row stays eligible on the next lease after a venue outage or archive transition.
    }
    if (terminal === "PENDING") return false;
    const updated = await db.query(
      "UPDATE cauche_free.research_settlements SET status = $3, result = $4, settled_at = $5, observed_at = $6, "
      + "source = $7 WHERE ticker = $1 AND contract_key = $2 AND attempts = $8 AND lease_id = $9 "
      + "AND status = 'PENDING'",
      [row.ticker, row.contract_key, terminal, resolution?.result ?? null, resolution?.settled_at ?? null,
        observedAt, source, row.attempts, lease],
    );
    return terminal === "SETTLED" && updated.rowCount === 1;
  }));
  return { processed: rows.length, settled: results.filter(Boolean).length };
}

export async function settlementStatus(db) {
  const result = { pending: 0, settled: 0, rules_changed: 0, unavailable: 0 };
  const { rows } = await db.query("SELECT status, count(*) AS n FROM cauche_free.research_settlements GROUP BY status");
  for (const row of rows) result[row.status.toLowerCase()] = Number(row.n);
  return result;
}
