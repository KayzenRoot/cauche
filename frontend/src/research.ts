export type Regime = 'trend' | 'breakout' | 'range' | 'unknown'
export type Venue = 'DERIV' | 'KALSHI'

export interface ResearchRecord {
  id: string
  decision_at: string
  observed_at: string
  settled_at: string | null
  venue: Venue
  instrument: string
  strategy: string
  regime: Regime
  event_id: string
  probability: number
  cost: number
  payout: number
  fees: number
  slippage: number
  market_probability?: number
  outcome: 0 | 1 | null
  fill_ratio?: number
  execution_price?: number
  execution_ms?: number
  rejected?: boolean
  source: string
}
export interface Dataset {
  version: 1
  currency: string
  records: ResearchRecord[]
}

export interface ResearchLimits {
  minimumEdge: number
  exposure: number
  eventExposure: number
  strategyExposure: number
  dailyLoss: number
  additionalSlippage: number
}
export const initialLimits = { minimumEdge: 0.03, exposure: 5, eventExposure: 2, strategyExposure: 3, dailyLoss: 2, additionalSlippage: 0.01 }
const MAX_ROWS = 2500
const regimes = new Set(['trend', 'breakout', 'range', 'unknown'])
const text = (value: unknown, max: number): value is string => typeof value === 'string' && Boolean(value.trim()) && value.length <= max
const recordObject = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const finite = (value: unknown, minimum: number, maximum: number): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum
const timestamp = (value: unknown): value is string => typeof value === 'string'
  && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value)
  && Number.isFinite(Date.parse(value))

export function parseDataset(input: unknown): Dataset {
  if (!recordObject(input) || input.version !== 1 || typeof input.currency !== 'string'
    || !/^[A-Z]{3}$/.test(input.currency) || !Array.isArray(input.records) || !input.records.length || input.records.length > MAX_ROWS) {
    throw new Error('Use um arquivo de evidências v1, com uma moeda e entre 1 e 2.500 registros.')
  }
  const ids = new Set<string>()
  const records = input.records.map((value, index): ResearchRecord => {
    const fail = () => new Error('Evidência inválida ou fora da ordem temporal na linha ' + (index + 1) + '.')
    if (!recordObject(value)) throw fail()
    const { id, decision_at, observed_at, settled_at, venue, instrument, strategy, regime, event_id,
      probability, cost, payout, fees, slippage, market_probability, outcome, fill_ratio,
      execution_price, execution_ms, rejected, source } = value
    if (!text(id, 160) || ids.has(id) || !timestamp(decision_at) || !timestamp(observed_at) || Date.parse(observed_at) > Date.parse(decision_at)
      || (settled_at !== null && (!timestamp(settled_at) || Date.parse(settled_at) <= Date.parse(decision_at)))
      || (venue !== 'DERIV' && venue !== 'KALSHI') || !text(instrument, 160) || !text(strategy, 160)
      || typeof regime !== 'string' || !regimes.has(regime) || !text(event_id, 160) || !text(source, 300)
      || !finite(probability, 0, 1) || !finite(cost, 0.000001, 1e7) || !finite(payout, 0.000001, 1e7)
      || !finite(fees, 0, 1e7) || !finite(slippage, 0, 1e7) || (market_probability !== undefined && !finite(market_probability, 0, 1))
      || (outcome !== null && outcome !== 0 && outcome !== 1) || ((outcome === null) !== (settled_at === null))
      || (fill_ratio !== undefined && !finite(fill_ratio, 0, 1)) || (execution_price !== undefined && !finite(execution_price, 0, 1e7))
      || (execution_ms !== undefined && !finite(execution_ms, 0, 86400000)) || (rejected !== undefined && typeof rejected !== 'boolean')
      || (rejected === true && fill_ratio !== undefined && fill_ratio !== 0)) throw fail()
    ids.add(id)
    return { id, decision_at, observed_at, settled_at, venue, instrument, strategy, regime: regime as Regime, event_id,
      probability, cost, payout, fees, slippage, market_probability, outcome, fill_ratio, execution_price, execution_ms, rejected, source }
  })
  records.sort((a, b) => Date.parse(a.decision_at) - Date.parse(b.decision_at) || a.id.localeCompare(b.id))
  return { version: 1, currency: input.currency, records }
}

export function sampleDataset(): Dataset {
  let seed = 14971
  const random = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0
    return seed / 4294967296
  }
  const origin = Date.UTC(2025, 8, 1)
  const iso = (ms: number) => new Date(ms).toISOString()
  const modes = ['trend', 'breakout', 'range'] as const
  const strategies = ['DERIV-STRAT-01', 'DERIV-STRAT-02', 'DERIV-STRAT-03']
  const records: ResearchRecord[] = Array.from({ length: 144 }, (_, index) => {
    const decision = origin + index * 90 * 60_000
    const venue = index % 4 === 0 ? 'KALSHI' : 'DERIV'
    const probability = 0.36 + random() * 0.42
    const cost = 0.37 + random() * 0.25
    const regime = modes[index % modes.length]
    const outcome = random() < 0.47 + (probability - 0.47) * 0.55 + (regime === 'trend' ? 0.05 : 0) ? 1 : 0
    const fill_ratio = index % 17 === 0 ? 0 : index % 9 === 0 ? 0.5 : 1
    return {
      id: 'amostra-' + (index + 1), decision_at: iso(decision), observed_at: iso(decision - 4000),
      settled_at: iso(decision + (20 + index % 6 * 20) * 60_000), venue,
      instrument: venue === 'KALSHI' ? 'EVENTO-SINTETICO' : 'EUR/USD',
      strategy: venue === 'KALSHI' ? 'KALSHI-STRAT-01' : strategies[index % strategies.length],
      regime, event_id: 'evento-' + Math.floor(index / 3), probability, cost, payout: 1,
      fees: 0.015, slippage: 0.02, market_probability: venue === 'KALSHI' ? cost : undefined,
      outcome, fill_ratio, execution_price: cost + (random() - 0.2) * 0.04,
      execution_ms: 40 + Math.round(random() * 180), rejected: fill_ratio === 0, source: 'AMOSTRA SINTÉTICA',
    }
  })
  return { version: 1, currency: 'USD', records }
}

const clamp = (p: number) => Math.min(0.999, Math.max(0.001, p))
const sigmoid = (x: number) => 1 / (1 + Math.exp(-x))
const feature = (row: ResearchRecord) => {
  const p = clamp(row.probability)
  return [1, Math.log(p / (1 - p)), Number(row.regime === 'trend'), Number(row.regime === 'breakout'), Number(row.regime === 'range')]
}
type Prediction = (row: ResearchRecord) => number

function fitLogistic(rows: ResearchRecord[]): Prediction {
  if (rows.length < 24 || new Set(rows.map(row => row.outcome)).size < 2) return row => row.probability
  const vectors = rows.map(row => ({ x: feature(row), y: Number(row.outcome) }))
  const weights = [0, 1, 0, 0, 0]
  for (let epoch = 0; epoch < 90; epoch++) {
    const totals = [0, 0, 0, 0, 0]
    for (const { x, y } of vectors) {
      const residual = sigmoid(x.reduce((sum, value, i) => sum + value * weights[i], 0)) - y
      x.forEach((value, i) => { totals[i] += value * residual })
    }
    weights.forEach((weight, i) => { weights[i] -= 0.35 * (totals[i] / vectors.length + (i ? 0.025 * weight : 0)) })
  }
  return row => clamp(sigmoid(feature(row).reduce((sum, value, i) => sum + value * weights[i], 0)))
}

export interface Decision {
  row: ResearchRecord
  probability: number
  executableCost: number
  maxLoss: number
  expected: number
  reason: 'EDGE' | 'LOW_EDGE' | 'EVENT_LIMIT' | 'PORTFOLIO_LIMIT' | 'STRATEGY_LIMIT' | 'DAILY_LOSS'
  fill: number
  net: number | null
}
export interface Metrics {
  samples: number
  signals: number
  closed: number
  net: number
  drawdown: number
  brier: number | null
  rejected: number
  partial: number
  observedExecution: number
  meanLatency: number | null
  meanSlippage: number | null
  reliability: { bucket: string; count: number; predicted: number; observed: number }[]
}

const score = (predictions: { probability: number; outcome: number }[]) => predictions.length
  ? predictions.reduce((total, item) => total + (item.probability - item.outcome) ** 2, 0) / predictions.length : null

function measure(decisions: Decision[]): Metrics {
  const entered = decisions.filter(d => d.reason === 'EDGE')
  const closed = entered.filter(d => d.net !== null)
    .sort((a, b) => Date.parse(a.row.settled_at ?? '') - Date.parse(b.row.settled_at ?? ''))
  let net = 0
  let peak = 0
  let drawdown = 0
  for (const item of closed) {
    net += item.net ?? 0
    peak = Math.max(peak, net)
    drawdown = Math.max(drawdown, peak - net)
  }
  const predictions = decisions.flatMap(({ row, probability }) => row.outcome === null ? [] : [{ probability, outcome: row.outcome }])
  const average = (amounts: number[]) => amounts.length ? amounts.reduce((sum, n) => sum + n, 0) / amounts.length : null
  return {
    samples: decisions.length, signals: entered.length, closed: closed.length, net, drawdown, brier: score(predictions),
    rejected: entered.filter(d => d.row.rejected === true).length, partial: entered.filter(d => d.fill > 0 && d.fill < 1).length,
    observedExecution: entered.filter(d => d.row.rejected !== undefined || d.row.fill_ratio !== undefined || d.row.execution_price !== undefined).length,
    meanLatency: average(entered.flatMap(d => d.row.execution_ms === undefined ? [] : [d.row.execution_ms])),
    meanSlippage: average(entered.flatMap(d => d.row.execution_price === undefined || d.fill === 0 ? [] : [d.row.execution_price - d.row.cost])),
    reliability: Array.from({ length: 5 }, (_, index) => {
      const entries = predictions.filter(v => Math.min(4, Math.floor(v.probability * 5)) === index)
      return {
        bucket: index * 20 + '–' + (index + 1) * 20 + '%', count: entries.length,
        predicted: average(entries.map(v => v.probability)) ?? 0, observed: average(entries.map(v => v.outcome)) ?? 0,
      }
    }).filter(bucket => bucket.count > 0),
  }
}

function simulate(rows: { row: ResearchRecord; probability: number }[], limits: ResearchLimits): Decision[] {
  const exposure = new Map<string, number>()
  const byStrategy = new Map<string, number>()
  const dailyLoss = new Map<string, number>()
  const openPositions: { event: string; strategy: string; cost: number; loss: number; settleAt: number }[] = []
  let totalExposure = 0
  return rows.map(({ row, probability }) => {
    const strategy = row.venue + ':' + row.strategy
    const time = Date.parse(row.decision_at)
    const day = new Date(time).toISOString().slice(0, 10)
    for (let i = openPositions.length - 1; i >= 0; i--) {
      const open = openPositions[i]
      if (open.settleAt > time) continue
      totalExposure = Math.max(0, totalExposure - open.cost)
      exposure.set(open.event, Math.max(0, (exposure.get(open.event) ?? 0) - open.cost))
      byStrategy.set(open.strategy, Math.max(0, (byStrategy.get(open.strategy) ?? 0) - open.cost))
      const settlementDay = new Date(open.settleAt).toISOString().slice(0, 10)
      dailyLoss.set(settlementDay, (dailyLoss.get(settlementDay) ?? 0) + open.loss)
      openPositions.splice(i, 1)
    }
    const executableCost = row.cost + row.fees + row.slippage + limits.additionalSlippage
    const expected = probability * row.payout - executableCost
    const decision: Decision = { row, probability, executableCost, maxLoss: executableCost, expected, reason: 'EDGE', fill: 0, net: null }
    if (expected <= 0 || expected < limits.minimumEdge) decision.reason = 'LOW_EDGE'
    else if ((dailyLoss.get(day) ?? 0) >= limits.dailyLoss) decision.reason = 'DAILY_LOSS'
    else if ((exposure.get(row.event_id) ?? 0) + executableCost > limits.eventExposure) decision.reason = 'EVENT_LIMIT'
    else if ((byStrategy.get(strategy) ?? 0) + executableCost > limits.strategyExposure) decision.reason = 'STRATEGY_LIMIT'
    else if (totalExposure + executableCost > limits.exposure) decision.reason = 'PORTFOLIO_LIMIT'
    if (decision.reason !== 'EDGE') return decision
    decision.fill = row.rejected ? 0 : row.fill_ratio ?? 1
    if (decision.fill <= 0) return decision
    const filledCost = (row.execution_price ?? (row.cost + row.slippage)) + row.fees + limits.additionalSlippage
    totalExposure += filledCost * decision.fill
    exposure.set(row.event_id, (exposure.get(row.event_id) ?? 0) + filledCost * decision.fill)
    byStrategy.set(strategy, (byStrategy.get(strategy) ?? 0) + filledCost * decision.fill)
    decision.net = row.outcome === null ? null : (row.outcome * row.payout - filledCost) * decision.fill
    openPositions.push({
      event: row.event_id, strategy, cost: filledCost * decision.fill, loss: Math.max(0, -(decision.net ?? 0)),
      settleAt: row.settled_at === null ? Infinity : Date.parse(row.settled_at),
    })
    return decision
  })
}

function eventGroupStarts(rows: ResearchRecord[]): number[] {
  const lastObservation = new Map<string, number>()
  const timestamps = rows.map(row => Date.parse(row.decision_at))
  rows.forEach((row, index) => { lastObservation.set(row.event_id, index) })
  const starts: number[] = []
  let through = -1
  for (let index = 0; index < rows.length; index++) {
    if (index > through && (index === 0 || timestamps[index] > timestamps[index - 1])) starts.push(timestamps[index])
    // Keep every event and any overlapping event interval in the same chronological group.
    through = Math.max(through, lastObservation.get(rows[index].event_id) ?? index)
  }
  return starts
}

export function analyze(dataset: Dataset, limits: ResearchLimits) {
  if (Object.values(limits).some(value => !finite(value, 0, 1e7))
    || [limits.dailyLoss, limits.eventExposure, limits.strategyExposure, limits.exposure].some(value => value <= 0)) {
    throw new Error('Informe limites positivos e custos não negativos para o cenário.')
  }
  const distinctTimes = new Set(dataset.records.map(row => Date.parse(row.decision_at)))
  if (distinctTimes.size < 40) throw new Error('Use pelo menos 40 instantes de decisão distintos para separar treino, validação e teste final.')
  const groupStarts = eventGroupStarts(dataset.records)
  const groupCount = groupStarts.length
  if (groupCount < 5) throw new Error('Há poucos grupos de eventos independentes no tempo para reservar janelas cronológicas.')
  const finalStart = Math.floor(groupCount * 0.8)
  const first = Math.min(Math.floor(groupCount * 0.4), finalStart - 3)
  const second = Math.min(Math.max(first + 1, Math.floor(groupCount * 0.53)), finalStart - 2)
  const third = Math.min(Math.max(second + 1, Math.floor(groupCount * 0.66)), finalStart - 1)
  const boundaries = [first, second, third, finalStart]
  const allPredictions: { row: ResearchRecord; probability: number }[] = []
  const windows = boundaries.map((cut, index) => {
    const start = groupStarts[cut]
    const end = groupStarts[boundaries[index + 1]] ?? Infinity
    const test = dataset.records.filter(row => Date.parse(row.decision_at) >= start && Date.parse(row.decision_at) < end)
    const testEvents = new Set(test.map(row => row.event_id))
    const train = dataset.records.filter(row => row.settled_at !== null && Date.parse(row.settled_at) < start && !testEvents.has(row.event_id))
    const predict = fitLogistic(train)
    allPredictions.push(...test.map(row => ({ row, probability: predict(row) })))
    return { start, end, train: train.length, fitted: train.length >= 24 && new Set(train.map(row => row.outcome)).size >= 2, holdout: index === boundaries.length - 1 }
  })
  const baseline = simulate(allPredictions.map(({ row }) => ({ row, probability: row.probability })), limits)
  const candidate = simulate(allPredictions, limits)
  const summarize = (rows: Decision[]) => measure(rows)
  const groups = new Map<string, Decision[]>()
  for (const row of candidate) {
    const name = [row.row.venue, row.row.instrument, row.row.strategy, row.row.regime].join(' · ')
    const entries = groups.get(name) ?? []
    entries.push(row)
    groups.set(name, entries)
  }
  const market = candidate.flatMap(({ row }) => row.outcome === null || row.market_probability === undefined
    ? [] : [{ probability: row.market_probability, outcome: row.outcome }])
  return {
    baseline: summarize(baseline), candidate: summarize(candidate), marketBrier: score(market), marketSamples: market.length,
    candidateMarketBrier: score(candidate.flatMap(({ row, probability }) => row.outcome === null || row.market_probability === undefined
      ? [] : [{ probability, outcome: row.outcome }])),
    decisions: candidate, windows: windows.map(window => {
      const inWindow = ({ row }: Decision) => Date.parse(row.decision_at) >= window.start && Date.parse(row.decision_at) < window.end
      return { ...window, baseline: summarize(baseline.filter(inWindow)), candidate: summarize(candidate.filter(inWindow)) }
    }),
    groups: [...groups].map(([name, entries]) => ({ name, metrics: summarize(entries) }))
      .sort((a, b) => b.metrics.samples - a.metrics.samples || a.name.localeCompare(b.name)),
  }
}

export type Report = ReturnType<typeof analyze>
