import assert from 'node:assert/strict'
import { test } from 'node:test'
import { analyze, initialLimits, parseDataset, sampleDataset } from '../src/research.ts'

test('the sample round trips as documented and has exactly one final holdout', () => {
  const dataset = parseDataset(JSON.parse(JSON.stringify(sampleDataset())))
  const report = analyze(dataset, initialLimits)
  assert.equal(dataset.records.length, 144)
  assert.equal(report.windows.length, 4)
  assert.equal(report.windows.filter(window => window.holdout).length, 1)
  assert.equal(report.windows.at(-1)?.holdout, true)
  assert.ok(report.candidate.brier !== null)
  assert.ok(report.candidate.closed > 0)
  assert.ok(report.candidate.rejected > 0)
  assert.ok(report.candidate.partial > 0)
})

test('rejects future observations, duplicate IDs and outcomes without settlement', () => {
  const original = sampleDataset()
  const [one, ...rest] = original.records
  for (const row of [
    { ...one, observed_at: '2055-01-01T00:00:00Z' },
    { ...one, id: rest[0].id },
    { ...one, settled_at: null },
  ]) {
    assert.throws(() => parseDataset({ ...original, records: [row, ...rest] }), /Evidência inválida/)
  }
})

test('changing unlabeled and future settlements cannot alter earlier predictions', () => {
  const original = sampleDataset()
  const reference = analyze(original, initialLimits)
  const first = reference.windows[0]
  const adjusted = {
    ...original,
    records: original.records.map(row => row.settled_at && Date.parse(row.settled_at) >= first.start
      ? { ...row, outcome: (row.outcome === 1 ? 0 : 1) as 0 | 1 }
      : row),
  }
  const altered = analyze(adjusted, initialLimits)
  const early = (report: typeof reference) => report.decisions
    .filter(decision => Date.parse(decision.row.decision_at) < first.end)
    .map(({ probability }) => probability)
  assert.deepEqual(early(reference), early(altered))
})

test('positive expected value still respects the shared event exposure cap', () => {
  const original = sampleDataset()
  const dataset = {
    ...original,
    records: original.records.map((row, index) => ({
      ...row, cost: 0.4, payout: 1, fees: 0.01, slippage: 0.01, probability: 0.9,
      settled_at: null, outcome: null, execution_price: undefined, fill_ratio: 1, rejected: false,
      event_id: 'shared-event-' + Math.floor(index / 3),
    })),
  }
  const report = analyze(dataset, { ...initialLimits, eventExposure: 0.7 })
  assert.equal(report.decisions[0].reason, 'EDGE')
  assert.equal(report.decisions[0].maxLoss, 0.43000000000000005)
  assert.ok(report.decisions.some(decision => decision.reason === 'EVENT_LIMIT'))
  const firstEvent = report.decisions.filter(decision => decision.row.event_id === report.decisions[0].row.event_id)
  assert.deepEqual(firstEvent.map(decision => decision.reason), ['EDGE', 'EVENT_LIMIT', 'EVENT_LIMIT'])
  assert.equal(report.candidate.closed, 0)
})

test('overlapping event intervals stay together across all chronological boundaries', () => {
  const original = sampleDataset()
  const records = original.records.map((row, index) => {
    if (index === 108 || index === 121) return { ...row, event_id: 'overlap-a' }
    if (index === 112 || index === 130) return { ...row, event_id: 'overlap-b' }
    return row
  })
  const report = analyze({ ...original, records }, initialLimits)
  const start = Date.parse(records[108].decision_at)
  const end = Date.parse(records[130].decision_at)
  assert.ok(report.windows.some(window => window.start <= start && window.end > end))
  for (const window of report.windows) {
    const current = records.filter(row => Date.parse(row.decision_at) >= window.start && Date.parse(row.decision_at) < window.end)
    const present = new Set(current.map(row => row.event_id))
    assert.ok(records.every(row => !present.has(row.event_id)
      || (Date.parse(row.decision_at) >= window.start && Date.parse(row.decision_at) < window.end)))
  }
})
