import assert from "node:assert/strict";
import test from "node:test";
import { captureBook, journalPage, recordInspection } from "../src/free-research-journal.mjs";

test("records executable asks and their quantity using the opposite best bid", async () => {
  const captured = await captureBook("KXTEST-1", async (url, request) => {
    assert.equal(url, "https://external-api.kalshi.com/trade-api/v2/markets/KXTEST-1/orderbook?depth=5");
    assert.equal(request.method, "GET");
    assert.equal(request.headers.Authorization, undefined);
    return Response.json({ orderbook_fp: {
      yes_dollars: [["0.1800", "2.00"], ["0.2200", "10.20"]],
      no_dollars: [["0.6400", "5.00"], ["0.6850", "17.20"]],
    } });
  });
  assert.equal(captured.status, "OBSERVED");
  assert.deepEqual(captured.yes_ask, { price: "0.3150", quantity: "17.20" });
  assert.deepEqual(captured.no_ask, { price: "0.7800", quantity: "10.20" });
});

test("missing quotes remain unavailable and captures retain official provenance", async () => {
  const invalidBook = await captureBook("KXTEST-1", async () => Response.json({ different: true }));
  assert.equal(invalidBook.status, "UNAVAILABLE");
  const captured = [];
  const at = "2026-11-20T13:00:00Z";
  const source = "https://external-api.kalshi.com/trade-api/v2/markets/KXTEST-1";
  const result = {
    ticker: "KXTEST-1", observed_at: at, sources: [{ id: "kalshi", url: source }],
    rules: { primary: "Official rule", closes_at: null }, market: { event_ticker: "KXTEST" },
    model: null, extraction: null,
  };
  const db = { async query(sql, args) {
    if (sql.startsWith("INSERT")) {
      captured.push({ id: "1", ticker: args[0], observed_at: args[1], evidence: JSON.parse(args[2]) });
      return { rows: [{ id: "1" }] };
    }
    if (sql.startsWith("SELECT count")) return { rows: [{ n: 1 }] };
    assert.deepEqual(args, [25, 0]);
    return { rows: captured };
  } };
  assert.equal(await recordInspection(db, result, invalidBook), "1");
  const page = await journalPage(db, new URLSearchParams());
  assert.equal(page.total, 1);
  assert.equal(page.items[0].mode, "READ_ONLY");
  assert.equal(page.items[0].data.evidence.source, source);
  assert.equal(page.items[0].data.evidence.book.status, "UNAVAILABLE");
  assert.equal(page.items[0].data.evidence.outcome, null);
});
