import assert from "node:assert/strict";
import test from "node:test";
import { inspectKalshi, RESEARCH_MODEL } from "../src/free-research.mjs";

const marketUrl = "https://external-api.kalshi.com/trade-api/v2/markets/TEST-26";
const now = new Date("2026-11-20T12:00:00Z");
const rule = "The NOAA report is the resolution source.";
const market = { market: {
  ticker: "TEST-26", rules_primary: rule, rules_secondary: "",
  close_time: "2026-11-20T13:00:00Z",
} };

test("official public rules remain available without an AI key", async () => {
  const calls = [];
  const result = await inspectKalshi({ ticker: "test-26" }, "", async (url, init) => {
    calls.push([url, init.method]);
    assert.equal(init.headers.Authorization, undefined);
    return Response.json(market);
  }, now);
  assert.deepEqual(calls, [[marketUrl, "GET"]]);
  assert.equal(result.observed_at, now.toISOString());
  assert.equal(result.rules.primary, rule);
  assert.equal(result.model_status, "NOT_CONFIGURED");
  assert.equal(result.decision_status, "REVIEW_REQUIRED");
  assert.equal(result.extraction, null);
});

test("uses the exact Flash model and emits only cited evidence with human review", async () => {
  const calls = [];
  const result = await inspectKalshi({ ticker: "TEST-26", sources: [{
    url: "https://weather.gov/example", published_at: "2026-11-20T10:00:00Z", text: "Early observations were neutral.",
  }] }, "synthetic-api-key", async (url, init) => {
    calls.push(url);
    if (url === marketUrl) return Response.json(market);
    assert.equal(url, "https://api.deepseek.com/chat/completions");
    assert.equal(init.redirect, "manual");
    const input = JSON.parse(init.body);
    assert.equal(input.model, RESEARCH_MODEL);
    assert.deepEqual(input.response_format, { type: "json_object" });
    assert.equal(input.tools, undefined);
    assert.ok(!init.body.includes("synthetic-api-key"));
    return Response.json({ choices: [{ finish_reason: "stop", message: { content: JSON.stringify({
      event: "Weather", resolution_source: "NOAA report", deadline: "2026-11-20T13:00:00Z", ambiguous: false,
      probability: 0.95, action: "BUY", facts: [
        { source_id: "kalshi", quote: rule, claim: "The resolution source is NOAA." },
        { source_id: "source-1", quote: "fabricated", claim: "Made up" },
      ],
    }) } }] });
  }, now);
  assert.equal(calls.length, 2);
  assert.equal(result.model, "deepseek-flash");
  assert.equal(result.extraction.deadline, null);
  assert.equal(result.extraction.ambiguous, true);
  assert.equal(result.extraction.probability, undefined);
  assert.equal(result.extraction.action, undefined);
  assert.equal(result.extraction.facts.length, 1);
  assert.equal(result.decision_status, "REVIEW_REQUIRED");
});

test("future dates and invalid source URLs fail before network activity", async () => {
  const fetcher = async () => assert.fail("unexpected external call");
  for (const source of [
    { url: "javascript:alert(1)", published_at: "2026-11-20T10:00:00Z" },
    { url: "https://weather.gov/example", published_at: "2026-11-20T14:00:00Z" },
  ]) {
    await assert.rejects(inspectKalshi({ ticker: "TEST-26", sources: [{ ...source, text: "example" }] },
      "synthetic-api-key", fetcher, now), { status: 422, message: "INVALID_EVIDENCE" });
  }
});
