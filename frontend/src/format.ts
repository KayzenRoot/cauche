import type { DecimalValue, Exchange, MarketMode, RecordData, Scalar } from './types'

export const exchangeNames: Record<Exchange, string> = { DERIV: 'Deriv', KALSHI: 'Kalshi' }
export const modeNames: Record<MarketMode, string> = {
  REPLAY: 'Replay',
  PAPER: 'Paper',
  BROKER_DEMO: 'Demo oficial',
  READ_ONLY: 'Somente leitura',
}

export const fixtures: Record<Exchange, {id: string; label: string}> = {
  DERIV: { id: 'EUR/USD', label: 'EUR/USD' },
  KALSHI: { id: 'FIXTURE-TEMP-NYC', label: 'Temperatura em Nova York · fixture' },
}

const labels: Record<string, string> = {
  STOPPED: 'Parado', STARTING: 'Iniciando', RUNNING: 'Em execução',
  PAUSING: 'Pausando', PAUSED: 'Pausado', STOPPING: 'Encerrando', ERROR: 'Erro',
  RISK_BLOCKED: 'Bloqueado pelo risco', DISCONNECTED: 'Desconectado',
  STALE: 'Desatualizado', NOT_CONFIGURED: 'A configurar', AUTH_ERROR: 'Falha de autenticação',
  CONNECTED: 'Conectada', AUTHENTICATED: 'Autenticada', FRESH: 'Atualizado',
  ONLINE: 'Online', OFFLINE: 'Offline', UNVERIFIED: 'A verificar', VERIFIED: 'Verificada', SIMULATED: 'Simulado',
  READY: 'Disponível', HEALTHY: 'Operacional', OK: 'Operacional', ACTIVE: 'Ativo',
  DISABLED: 'Desativado', IDLE: 'Em espera', INFO: 'Informação', WARNING: 'Atenção',
  WARN: 'Atenção', CRITICAL: 'Crítico', UNKNOWN: 'Desconhecido', UNAVAILABLE: 'Indisponível',
  OPEN: 'Aberta', FILLED: 'Executada', PARTIAL: 'Parcial', PARTIALLY_FILLED: 'Parcial',
  CANCELLED: 'Cancelada', CANCELED: 'Cancelada', SETTLED: 'Liquidada', ACCEPTED: 'Aceita',
  SUBMITTED: 'Enviada', REJECTED: 'Rejeitada', COMPLETED: 'Concluído', FAILED: 'Falhou',
  FIXTURE: 'Fixture simulada', SIMULATED_FIXTURE: 'Fixture simulada', OFFLINE_FIXTURE: 'Fixture simulada',
  KALSHI_OFFICIAL_DEMO_MARKET: 'Kalshi · mercado demo oficial', KALSHI_OFFICIAL_PUBLIC: 'Kalshi · mercado público oficial',
  DERIV_OFFICIAL_PUBLIC: 'Deriv · mercado público oficial',
  CALL: 'Alta', PUT: 'Baixa', YES: 'Sim', NO: 'Não', BUY: 'Compra', SELL: 'Venda',
  MARKET: 'Mercado', STRATEGY: 'Estratégia', SIGNAL: 'Sinal', ORDER: 'Ordem', FILL: 'Execução',
  SETTLEMENT: 'Liquidação', RISK: 'Risco', API: 'API', AUTH: 'Autenticação',
  ADMIN: 'Administração', SYSTEM: 'Sistema', REPLAY: 'Replay', PAPER: 'Paper',
  BROKER_DEMO: 'Demo oficial', READ_ONLY: 'Somente leitura', DERIV: 'Deriv', KALSHI: 'Kalshi',
}

export const localText = (value?: string | null) => value ? labels[value.toUpperCase()] ?? value : '—'

export function numberValue(value: DecimalValue): number | undefined {
  if (value === null || value === undefined || value === '') return undefined
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : undefined
}

export function number(value: DecimalValue, digits = 2) {
  const numeric = numberValue(value)
  return numeric === undefined ? '—' : Intl.NumberFormat('pt-BR', { maximumFractionDigits: digits }).format(numeric)
}

export function money(value: DecimalValue, currency = 'USD') {
  const numeric = numberValue(value)
  return numeric === undefined ? '—' : Intl.NumberFormat('pt-BR', {
    style: 'currency', currency: /^[A-Z]{3}$/.test(currency) ? currency : 'USD',
    currencyDisplay: 'narrowSymbol',
  }).format(numeric)
}

export function percent(value: DecimalValue) {
  const numeric = numberValue(value)
  return numeric === undefined ? '—' : Intl.NumberFormat('pt-BR', {
    style: 'percent', maximumFractionDigits: 2,
  }).format(numeric)
}

export const timestamp = (value: string | number) =>
  typeof value === 'number' ? new Date(value * 1000) : new Date(value)

export function dateTime(value?: string | number | null) {
  if (value === null || value === undefined || value === '') return '—'
  const date = timestamp(value)
  if (!Number.isFinite(date.getTime())) return '—'
  return new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit',
    minute: '2-digit', second: '2-digit', timeZone: 'UTC', hourCycle: 'h23',
  }).format(date) + ' UTC'
}

export function relativeTime(value?: string | null) {
  if (!value) return 'Sem sincronização'
  const offset = (Date.parse(value) - Date.now()) / 1000
  if (!Number.isFinite(offset)) return 'Sem sincronização'
  const abs = Math.abs(offset)
  const formatter = new Intl.RelativeTimeFormat('pt-BR', { numeric: 'always' })
  if (abs < 60) return formatter.format(Math.round(offset), 'second')
  if (abs < 3600) return formatter.format(Math.round(offset / 60), 'minute')
  if (abs < 86400) return formatter.format(Math.round(offset / 3600), 'hour')
  return formatter.format(Math.round(offset / 86400), 'day')
}

export function scalar(value: unknown): Scalar {
  return value === null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? value : null
}

export function field(row: RecordData, ...keys: string[]) {
  for (const key of keys) {
    const value = scalar(row[key])
    if (value !== null) return value
  }
  return null
}

export function valueText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'Sim' : 'Não'
  if (typeof value === 'object') return JSON.stringify(value)
  return localText(String(value))
}

export type Tone = 'green' | 'lime' | 'amber' | 'red' | 'neutral' | 'blue'

export function statusTone(state?: string | null): Tone {
  const value = state?.toUpperCase() ?? ''
  if (['VERIFIED', 'RUNNING', 'CONNECTED', 'AUTHENTICATED', 'FRESH', 'OK', 'HEALTHY', 'FILLED', 'SETTLED', 'COMPLETED', 'ACTIVE', 'ONLINE'].includes(value)) return 'green'
  if (['PAUSING', 'PAUSED', 'STOPPING', 'STARTING', 'STALE', 'WARNING', 'WARN', 'NOT_CONFIGURED', 'PARTIAL', 'UNKNOWN', 'UNVERIFIED'].includes(value)) return 'amber'
  if (['ERROR', 'RISK_BLOCKED', 'AUTH_ERROR', 'DISCONNECTED', 'CRITICAL', 'FAILED', 'REJECTED', 'UNAVAILABLE', 'OFFLINE'].includes(value)) return 'red'
  return 'neutral'
}

export function strategyName(id?: string | null, fallback?: string): string {
  if (!id) return '—'
  return ({
    'DERIV-STRAT-01': 'Tendência e correção',
    'DERIV-STRAT-02': 'Rompimento com ATR',
    'DERIV-STRAT-03': 'Reversão com Bollinger e RSI',
    'KALSHI-STRAT-01': 'Vantagem probabilística',
    'KALSHI-STRAT-02': 'Formação de mercado',
    'KALSHI-STRAT-03': 'Arbitragem lógica',
  } as Record<string, string>)[id] ?? fallback ?? id
}

export function marketSource(source?: string | null) {
  if (!source) return 'Fonte indisponível'
  if (/fixture|simulat/.test(source.toLowerCase())) return 'Fixture simulada'
  return localText(source)
}

export function instrumentLabel(instrument: string) {
  if (instrument.startsWith('FIXTURE:')) return instrument.slice('FIXTURE:'.length) + ' · fixture'
  if (instrument === fixtures.KALSHI.id) return 'Temperatura · Nova York'
  if (instrument === 'frxEURUSD') return 'EUR/USD'
  if (instrument === 'frxGBPUSD') return 'GBP/USD'
  if (instrument === 'frxUSDJPY') return 'USD/JPY'
  return instrument
}

export function eventMessage(code: string, fallback: string) {
  if (code.startsWith('BOT_') && fallback.startsWith('Execution state changed')) {
    return 'Estado do motor atualizado para ' + localText(code.slice(4)).toLowerCase() + '.'
  }
  return ({
    MARKET_OBSERVED: 'Observação de mercado registrada.',
    ORDER_PROPOSED: 'Intenção de ordem registrada.',
    ORDER_ACKNOWLEDGED: 'A corretora confirmou o recebimento da ordem.',
    PRE_DISPATCH_CANCELLED: 'A ordem perdeu a elegibilidade antes do envio.',
    ORDER_RECONCILED: 'Estado da ordem atualizado pela corretora.',
    ORDER_CANCELLED: 'Ordem cancelada pelo motor.',
    ORDER_EXPIRED: 'Ordem simulada expirou sem execução elegível.',
    FILL_CONFIRMED: 'Execução registrada.',
    SETTLED: 'Liquidação registrada.',
    SUBMISSION_UNKNOWN: 'Envio anterior requer reconciliação com a corretora.',
    SIMULATION_BUDGET_CHANGED: 'O custo excedeu o valor reservado.',
    FIXTURE_ENDED_WITH_OPEN_POSITIONS: 'A série terminou com posições sem liquidação observada.',
  } as Record<string, string>)[code] ?? fallback
}
