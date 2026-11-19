export type Exchange = 'DERIV' | 'KALSHI'
export type Mode = 'REPLAY' | 'PAPER' | 'BROKER_DEMO'
export type MarketMode = Mode | 'READ_ONLY'
export type Scalar = string | number | boolean | null
export type DecimalValue = string | number | null | undefined
export type RecordData = Record<string, unknown>

export interface Session {
  email: string
  role: string
  csrf_token: string
}

export interface Scope {
  exchange: Exchange
  mode: MarketMode
  strategy: string
  instrument: string
  start: string
  end: string
}

export interface Bot {
  id: number
  state: string
  desired_state: string
  mode: Mode | null
  exchange: Exchange | null
  strategy: string | null
  instrument: string | null
  run_id: string | number | null
  cursor: number
  heartbeat_at: string | null
  updated_at: string | null
  last_error: string | null
}

export interface EventRecord {
  id: number
  timestamp: string
  type: string
  severity: string
  code: string
  message: string
  exchange?: Exchange | null
  mode?: Mode | null
  strategy?: string | null
  instrument?: string | null
  correlation_id?: string | null
  order_id?: string | null
  context?: RecordData | null
}

export interface EventsPage {
  items: EventRecord[]
  next_cursor: number | null
  before: number | null
  has_more: boolean
}

export interface List<T> {
  items: T[]
}

export interface ParameterDefinition {
  type?: string
  default?: Scalar
  minimum?: number | string
  maximum?: number | string
  min?: number | string
  max?: number | string
  description?: string
  title?: string
}

export interface Strategy {
  id: string
  exchange: Exchange
  name: string
  version: string
  enabled: boolean
  parameters: Record<string, Scalar>
  parameter_schema?: Record<string, ParameterDefinition>
}

export interface Candle {
  time: number
  open: string | number
  high: string | number
  low: string | number
  close: string | number
  volume?: DecimalValue
}

export interface Marker {
  time: number
  type?: string
  kind?: string
  label?: string
  text?: string
}

export interface Market {
  source: string
  status: string
  exchange: Exchange
  instrument: string
  last_sync: string | null
  mark: DecimalValue
  candles: Candle[]
  markers?: Marker[]
  timeframes?: number[]
  timeframe?: number
  native_timeframe?: number
  book?: RecordData | null
  contracts?: RecordData[] | null
}

export interface Instrument {
  id: string
  label: string
  source: string
}

export interface Connection {
  exchange: Exchange
  environment: 'BROKER_DEMO'
  status: string
  enabled: boolean
  last_sync: string | null
  capabilities: string[] | RecordData
  configured: boolean
}

export interface Dashboard {
  mode: MarketMode
  exchange: Exchange
  instrument: string
  metrics: Record<string, DecimalValue>
  orders: RecordData[]
  positions: RecordData[]
  equity: Array<{time: number | string; value: string | number}>
  events: EventRecord[]
  bot: Bot
  mode_summaries?: RecordData[]
  strategy_summaries?: RecordData[]
  connections: Connection[]
  period_results?: Record<'daily' | 'weekly' | 'monthly', RecordData[]>
}

export interface History {
  orders: RecordData[]
  fills: RecordData[]
  positions: RecordData[]
  settlements: RecordData[]
  fees: RecordData[]
}

export interface Backtest {
  id: number | string
  status: string
  exchange: Exchange
  strategy: string
  instrument: string
  source: string
  result: RecordData | null
  created_at: string
}

export interface RiskConfig {
  kill_switch: boolean
  thresholds: Record<string, Scalar>
}

export interface Overview {
  status: string
  version: string
  database: string | RecordData
  worker: string | RecordData
  uptime_seconds: number
  log_retention_days?: number
  bot: Bot
  connections: Connection[]
  events: EventRecord[]
}
