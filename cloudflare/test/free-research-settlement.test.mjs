import assert from "node:assert/strict";
import test from "node:test";
import { contractKey, finalResult, processSettlements, enqueueSql } from "../src/free-research-settlement.mjs";

const ticker = "KXTEST-2";
const source = "https://external-api.kalshi.com/trade-api/v2/markets/" + ticker;
const first = {
  event_ticker: "KXTEST", primary: "Official rule", secondary: "Alternate rule",
  closes_at: "2026-11-10T01:00:00Z", source,
};
const final = { ...first, status: "finalized", result: "yes", settled_at: "2026-11-10T02:00:00Z" };

test("an outcome requires a final binary settlement and a stable contract", async () => {
  assert.equal(await contractKey(ticker, { ...first, closes_at: "2026-11-09T20:00:00-05:00" }),
    await contractKey(ticker, first));
  assert.notEqual(await contractKey(ticker, { ...first, primary: "New rule" }), await contractKey(ticker, first));
  assert.equal(await contractKey(ticker, { ...first, closes_at: null }), null);
  const observed = "2026-11-10T03:00:00Z";
  assert.equal(finalResult({ ...final, status: "determined" }, observed), null);
  assert.equal(finalResult({ ...final, settled_at: "2026-11-11T00:00:00Z" }, observed), null);
  assert.equal(finalResult({ ...final, settled_at: null }, observed), null);
  assert.deepEqual(finalResult(final, observed), { result: "YES", settled_at: "2026-11-10T02:00:00.000Z" });
});

test("capture enqueues the exact contract and preserves independent source timestamps", async () => {
  const prior = { ticker, rules: first, market: { event_ticker: first.event_ticker },
    observed_at: "2026-11-09T12:00:00Z", sources: [{ url: source }] };
  const bookAt = "2026-11-09T12:00:02Z";
  const id = await enqueueSql({
    async query(sql, args) {
      assert.ok(sql.startsWith("WITH queued AS"));
      assert.match(sql, /ON CONFLICT \(ticker, contract_key\)/);
      assert.equal(args[1], bookAt);
      assert.equal(JSON.parse(args[2]).contract_key, await contractKey(ticker, first));
      assert.equal(args[5], "PENDING");
      assert.equal(args[6], null);
      assert.equal(JSON.parse(args[10]).primary, first.primary);
      return { rows: [{ id: "1" }] };
    },
  }, prior, { book: { observed_at: bookAt } });
  assert.equal(id, "1");
});

test("leased due checks persist exact results and surface changed terms separately", async () => {
  const key = await contractKey(ticker, first);
  const writes = [];
  let lease;
  const db = { async query(sql, args) {
    if (sql.startsWith("SELECT DISTINCT")) return { rows: [] };
    if (sql.startsWith("WITH due")) {
      assert.equal(args[0], 3);
      [ , lease ] = args;
      assert.match(sql, /SKIP LOCKED/);
      return { rows: [{ ticker, contract_key: key, attempts: 1 }, { ticker: "KXOTHER", contract_key: key, attempts: 1 }] };
    }
    assert.equal(args[8], lease);
    assert.match(sql, /lease_id = \$9/);
    writes.push(args);
    return { rowCount: 1 };
  } };
  const result = await processSettlements(db, async id => id === ticker ? final : { ...final, primary: "Changed" });
  assert.deepEqual(result, { processed: 2, settled: 1 });
  assert.deepEqual(writes.find(args => args[0] === ticker).slice(2, 5),
    ["SETTLED", "YES", "2026-11-10T02:00:00.000Z"]);
  assert.deepEqual(writes.find(args => args[0] === "KXOTHER").slice(2, 5), ["RULES_CHANGED", null, null]);
});
