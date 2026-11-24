import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, ArrowDownRight, ArrowUpRight, BarChart3, CandlestickChart, ChevronRight, CircleDot, Clock3,
  Crosshair, Gauge, Pause, Play, Radio, ShieldCheck, Square, Wallet,
} from 'lucide-react'
import { api, params, scopeParams } from '../api'
import type { StreamStatus } from '../api'
import { ConfirmDialog } from '../components/confirm'
import type { Confirmation } from '../components/confirm'
import { Badge, DataTable, Empty, Feed, Loading, PageTitle, Problem, Status } from '../components/common'
import type { Column } from '../components/common'
import { EquityGraph, MarketChart } from '../components/chart'
import { ScopeFields } from '../components/scope'
import {
  dateTime, exchangeNames, field, instrumentLabel, localText, marketSource, modeNames, money,
  number, numberValue, percent, relativeTime, scalar, strategyName, valueText,
} from '../format'
import type { Bot, Dashboard, Market, Scope, Session, Strategy } from '../types'

const TIMEFRAMES = [{ seconds: 60, label: 'M1' }, { seconds: 300, label: 'M5' },
  { seconds: 900, label: 'M15' }, { seconds: 1800, label: 'M30' }, { seconds: 3600, label: 'H1' },
  { seconds: 14400, label: 'H4' }, { seconds: 86400, label: 'D1' }]
const canStop = ['STARTING', 'RUNNING', 'PAUSED', 'PAUSING', 'RISK_BLOCKED', 'ERROR', 'DISCONNECTED']
const freshStatuses = ['FRESH', 'READY', 'CONNECTED', 'LIVE', 'SIMULATED']
type BotAction = 'start' | 'pause' | 'resume' | 'stop'

function ExecutionPanel({
  bot, dashboard, market, scope, session, strategies, isLoading, stream,
}: {
  bot?: Bot; dashboard?: Dashboard; market?: Market; scope: Scope; session: Session; strategies: Strategy[];
  isLoading: boolean; stream: StreamStatus;
}) {
  const [confirmation, setConfirmation] = useState<Confirmation>()
  const client = useQueryClient()
  const action = useMutation({
    mutationFn: (kind: BotAction) => api<Bot>('/bot/' + kind, {
      method: 'POST', session,
      body: kind === 'start'
        ? { mode: scope.mode, exchange: scope.exchange, strategy: scope.strategy, instrument: scope.instrument, confirm: scope.mode === 'BROKER_DEMO' }
        : undefined,
    }),
    onSuccess: result => {
      client.setQueryData(['bot'], result)
      for (const key of ['dashboard', 'history', 'events-live', 'admin-overview']) void client.invalidateQueries({ queryKey: [key] })
    },
  })
  const selected = strategies.find(item => item.id === scope.strategy)
  const connection = dashboard?.connections.find(item => item.exchange === scope.exchange)
  const state = bot?.state
  const active = state !== undefined && !['STOPPED', 'ERROR', 'DISCONNECTED'].includes(state)
  const aligned = bot?.mode === scope.mode && bot?.exchange === scope.exchange && bot?.instrument === scope.instrument
  const demoFixture = scope.mode === 'BROKER_DEMO' && /^(FIXTURE|EUR\/USD$|GBP\/USD$|USD\/JPY$)/i.test(scope.instrument)
  const demoUnavailable = scope.mode === 'BROKER_DEMO' && (connection?.configured !== true || !connection.enabled || connection.status !== 'CONNECTED')
  const canStart = ['STOPPED', 'ERROR'].includes(state ?? '') && bot?.desired_state === 'STOPPED' && scope.mode !== 'READ_ONLY'
    && Boolean(scope.instrument) && selected?.enabled === true && !demoUnavailable && !demoFixture
  const send = (kind: BotAction) => {
    if (kind === 'stop') {
      setConfirmation({
        title: 'Encerrar operação?',
        description: 'O motor receberá a solicitação de encerramento. Acompanhe o estado e as ordens pendentes no terminal.',
        destructive: true, confirm: () => action.mutate(kind),
      })
    } else if (kind === 'start' && scope.mode === 'BROKER_DEMO') {
      setConfirmation({
        title: 'Iniciar na demo oficial?',
        description: 'A estratégia selecionada poderá enviar ordens para o ambiente de demonstração da corretora.',
        confirm: () => action.mutate(kind),
      })
    } else action.mutate(kind)
  }
  const metrics = dashboard?.metrics
  const marketFresh = stream !== 'offline' && freshStatuses.includes(market?.status ?? '')
  const quotes = marketFresh && (!active || aligned) ? market?.contracts ?? [] : []
  const payoutLabels = quotes.slice(0, 2).map(quote => localText(String(quote.side)) + ' ' + money(scalar(quote.payout) as string))
  return <section className="panel execution" aria-label="Execução">
    <div className="panel-heading">
      <div className="section-name"><span className="panel-icon"><Crosshair size={19} aria-hidden="true" /></span>
        <div><h2>Execução</h2><p>Controle do motor</p></div>
      </div>
      <span role="status" aria-label="Estado do motor"><Status state={state} /></span>
    </div>
    <dl className="run-details">
      <div><dt>Corretora</dt><dd>{localText(active ? bot?.exchange : scope.exchange)}</dd></div>
      <div><dt>Ambiente</dt><dd>{localText(active ? bot?.mode : scope.mode)}</dd></div>
      <div><dt>Ativo</dt><dd>{instrumentLabel((active ? bot?.instrument : scope.instrument) || '—')}</dd></div>
      <div><dt>Estratégia</dt><dd>{strategyName(active ? bot?.strategy : selected?.id)}</dd></div>
      <div><dt>Exposição</dt><dd>{!active || aligned ? money(metrics?.exposure, String(metrics?.currency ?? 'USD')) : '—'}</dd></div>
      <div><dt>{scope.exchange === 'KALSHI' ? 'Preço do contrato' : 'Payout observado'}</dt>
        <dd>{scope.exchange === 'KALSHI'
          ? (!active || aligned) && marketFresh ? number(market?.mark, 4) : '—'
          : payoutLabels.join(' · ') || '—'}</dd></div>
      <div><dt>Ordens abertas</dt><dd>{!active || aligned ? number(metrics?.open_orders, 0) : '—'}</dd></div>
      <div><dt>Resultado líquido</dt><dd>{!active || aligned ? money(metrics?.net_pnl, String(metrics?.currency ?? 'USD')) : '—'}</dd></div>
    </dl>
    <div className="execute-buttons">
      <button type="button" className="button-primary" onClick={() => send('start')} disabled={!canStart || action.isPending || isLoading}>
        <Play size={16} aria-hidden="true" /> Iniciar operação
      </button>
      <button type="button" onClick={() => send('pause')} disabled={state !== 'RUNNING' || action.isPending}><Pause size={15} aria-hidden="true" /> Pausar operação</button>
      <button type="button" onClick={() => send('resume')} disabled={state !== 'PAUSED' || action.isPending}><Play size={15} aria-hidden="true" /> Retomar operação</button>
      <button type="button" className="stop-button" onClick={() => send('stop')} disabled={!canStop.includes(state ?? '') || action.isPending}><Square size={15} aria-hidden="true" /> Encerrar operação</button>
    </div>
    {action.isError && <Problem error={action.error} />}
    {bot?.last_error && <p className="run-alert" role="status"><ShieldCheck size={16} aria-hidden="true" /> {localText(bot.last_error)}</p>}
    {!active && !selected && <p className="hint">Selecione uma estratégia para iniciar.</p>}
    {!active && selected && !selected.enabled && <p className="hint">Ative esta estratégia na <Link to="/admin/estrategias">administração</Link>.</p>}
    {!active && demoUnavailable && <p className="hint">Configure e teste a demo em <Link to="/admin/conexoes">Conexões</Link>.</p>}
    {demoFixture && <p className="hint">Informe um identificador de mercado da corretora para operar em demo.</p>}
    {confirmation && <ConfirmDialog value={confirmation} onClose={() => setConfirmation(undefined)} />}
  </section>
}

function bookQuote(value: unknown, complement = false) {
  const numeric = numberValue(scalar(value) as string)
  if (numeric === undefined || numeric < 0 || numeric > 1) return '—'
  const units = Math.round(numeric * 10_000)
  return number((complement ? 10_000 - units : units) / 10_000, 4)
}

function MetricStrip({ dashboard, loading }: {dashboard?: Dashboard; loading: boolean}) {
  const metrics = dashboard?.metrics
  const currency = String(metrics?.currency ?? 'USD')
  const balanceCurrency = String(metrics?.balance_currency ?? currency)
  const net = numberValue(metrics?.net_pnl)
  const pnlTone = net !== undefined && net !== 0 ? (net > 0 ? 'positive' : 'negative') : ''
  const stats = [
    { label: 'Saldo disponível', value: money(metrics?.balance, balanceCurrency), note: 'No escopo selecionado', Icon: Wallet },
    { label: 'Patrimônio', value: money(metrics?.equity, balanceCurrency), note: 'Saldo e posições', Icon: Activity },
    { label: 'Resultado líquido', value: money(metrics?.net_pnl, currency), note: 'Após taxas', Icon: net !== undefined && net < 0 ? ArrowDownRight : ArrowUpRight, tone: pnlTone },
    { label: 'Operações', value: number(metrics?.trade_count, 0), note: numberValue(metrics?.win_rate) === undefined ? 'Taxa de acerto indisponível' : percent(metrics?.win_rate) + ' de acerto', Icon: BarChart3 },
  ]
  return <div className="metric-strip" aria-busy={loading}>
    {stats.map(({ label, value, note, Icon, tone }) => <section className="panel metric" key={label}>
      <div className="metric-heading"><h2>{label}</h2><Icon size={17} aria-hidden="true" /></div>
      <strong className={tone}>{value}</strong>
      <span>{note}</span>
    </section>)}
  </div>
}

function MarketPanel({
  market, error, retry, loading, scope, timeframe, setTimeframe, stream, terminal,
}: {
  market?: Market; error?: Error | null; retry: () => void; loading: boolean; scope: Scope; timeframe: number;
  setTimeframe: (value: number) => void; stream: StreamStatus; terminal: boolean;
}) {
  const status = market?.status
  const fixtures = /fixture|simulat/.test(market?.source?.toLowerCase() ?? '')
  const historical = scope.mode === 'REPLAY' || fixtures
  const lost = stream === 'offline'
  const showMark = market && !error && !lost && (historical || freshStatuses.includes(status ?? ''))
  const supported = market?.timeframes
  const nativeTimeframe = market?.native_timeframe ?? (scope.exchange === 'KALSHI' ? 60 : 300)
  const timeframes = supported?.length
    ? TIMEFRAMES.filter(time => supported.includes(time.seconds))
    : TIMEFRAMES.filter(time => time.seconds >= nativeTimeframe)
  const candles = market?.candles ?? []
  const mark = market?.mark ?? candles.at(-1)?.close
  return <section className={'panel market-panel' + (terminal ? ' market-terminal' : '')} aria-label="Mercado">
    <div className="panel-heading market-heading">
      <div className="section-name"><span className="panel-icon"><CandlestickChart size={20} aria-hidden="true" /></span>
        <div><h2>{market?.instrument || scope.instrument ? instrumentLabel(market?.instrument || scope.instrument) : 'Mercado'}</h2>
          <p>{exchangeNames[scope.exchange]} <i /> {marketSource(market?.source)}</p></div>
      </div>
      <div className="market-price"><strong>{showMark ? number(mark, scope.exchange === 'DERIV' ? 5 : 4) : '—'}</strong>
        <span>{historical ? 'Último preço da série' : 'Preço observado'}</span></div>
    </div>
    <div className="market-toolbar">
      <div className="segmented" aria-label="Intervalo do gráfico">
        {timeframes.map(item => <button key={item.seconds} type="button" aria-pressed={timeframe === item.seconds} onClick={() => setTimeframe(item.seconds)}>{item.label}</button>)}
      </div>
      <Status state={lost ? 'DISCONNECTED' : error && status ? 'STALE' : status} />
    </div>
    {error && <Problem error={error} retry={retry} />}
    {loading ? <Loading /> : candles.length ? <MarketChart
      candles={candles} markers={market?.markers} exchange={scope.exchange} timeframe={timeframe}
      identity={[scope.exchange, scope.instrument, scope.mode, timeframe, scope.start, scope.end].join(':')} />
      : !error && <Empty icon="market" title="Sem candles para este filtro" description="Selecione um ativo e período com dados disponíveis." />}
    <div className="market-footer">
      <span><Radio size={14} aria-hidden="true" /> {marketSource(market?.source)}</span>
      <span><Clock3 size={14} aria-hidden="true" /> {dateTime(market?.last_sync)}</span>
    </div>
    {showMark && market?.book && <details className="market-depth form-details">
      <summary>Livro de ofertas observado</summary>
      <DataTable title="Melhores preços de compra e venda" empty="Sem ofertas"
        rows={[
          { side: 'Sim', bid: bookQuote(market.book.yes_bid), ask: bookQuote(market.book.yes_ask),
            bidSize: number(scalar(market.book.yes_bid_size) as string), askSize: number(scalar(market.book.yes_ask_size) as string) },
          { side: 'Não', bid: bookQuote(market.book.yes_ask, true), ask: bookQuote(market.book.yes_bid, true),
            bidSize: number(scalar(market.book.yes_ask_size) as string), askSize: number(scalar(market.book.yes_bid_size) as string) },
        ]}
        columns={[
          { key: 'side', label: 'Lado', value: row => valueText(row.side) },
          { key: 'bid', label: 'Compra (USD)', value: row => valueText(row.bid), numeric: true },
          { key: 'bidSize', label: 'Qtd. compra', value: row => valueText(row.bidSize), numeric: true },
          { key: 'ask', label: 'Venda (USD)', value: row => valueText(row.ask), numeric: true },
          { key: 'askSize', label: 'Qtd. venda', value: row => valueText(row.askSize), numeric: true },
        ]} />
    </details>}
    {showMark && Array.isArray(market?.contracts) && market.contracts.length > 0 && <details className="market-depth form-details">
      <summary>Cotações de contratos disponíveis</summary>
      <DataTable title="Cotações de contratos" empty="Sem contratos" rows={market.contracts}
        columns={[
          { key: 'side', label: 'Lado', value: row => valueText(row.side) },
          { key: 'stake', label: 'Valor', value: row => money(scalar(row.stake) as string), numeric: true },
          { key: 'payout', label: 'Pagamento potencial', value: row => money(scalar(row.payout) as string), numeric: true },
          { key: 'duration', label: 'Duração (s)', value: row => number(scalar(row.duration_seconds) as string, 0), numeric: true },
        ]} />
    </details>}
  </section>
}

const summaryColumns: Column[] = [
  { key: 'name', label: 'Estratégia / corretora', value: row => {
    const strategy = field(row, 'strategy', 'strategy_id')
    return strategy ? strategyName(String(strategy)) : valueText(field(row, 'exchange', 'name'))
  } },
  { key: 'mode', label: 'Ambiente', value: row => valueText(row.mode) },
  { key: 'trades', label: 'Operações', value: row => number(scalar(row.trade_count ?? row.trades) as string), numeric: true },
  { key: 'expectancy', label: 'Expectativa', value: row => money(scalar(row.expectancy) as string, String(row.currency ?? 'USD')), numeric: true },
  { key: 'net', label: 'Resultado líquido', value: row => money(scalar(row.net_pnl) as string, String(row.currency ?? 'USD')), numeric: true },
]

export function TradingPage({
  terminal, scope, update, strategies, session, stream,
}: {
  terminal: boolean; scope: Scope; update: (value: Scope) => void;
  strategies: Strategy[]; session: Session; stream: StreamStatus;
}) {
  const [timeframe, setTimeframe] = useState(300)
  const [showAllMetrics, setShowAllMetrics] = useState(false)
  const scoped = scopeParams(scope)
  const dashboard = useQuery({
    queryKey: ['dashboard', scoped],
    queryFn: ({ signal }) => api<Dashboard>('/dashboard' + params(scoped), { signal }),
    enabled: scope.mode !== 'READ_ONLY',
    refetchInterval: stream === 'online' ? 15000 : 7000,
  })
  const market = useQuery({
    queryKey: ['market', scoped, timeframe],
    queryFn: ({ signal }) => api<Market>('/market' + params({ ...scoped, timeframe,
      mode: scope.mode === 'READ_ONLY' ? 'PAPER' : scope.mode }), { signal }),
    enabled: Boolean(scope.instrument) && (scope.mode !== 'READ_ONLY' || !scope.instrument.startsWith('FIXTURE')),
    refetchInterval: stream === 'online' ? 15000 : 7000,
  })
  const bot = useQuery({
    queryKey: ['bot'], queryFn: ({ signal }) => api<Bot>('/bot', { signal }),
    refetchInterval: 5000,
  })
  const metrics = dashboard.data?.metrics
  const currency = String(metrics?.currency ?? 'USD')
  const balanceCurrency = String(metrics?.balance_currency ?? currency)
  const metricDetail = [
    ['Resultado realizado', money(metrics?.realized_pnl, currency)],
    ['Resultado não realizado', money(metrics?.unrealized_pnl, balanceCurrency)],
    ['Resultado bruto', money(metrics?.gross_pnl, currency)],
    ['Taxas pagas', money(metrics?.fees, currency)],
    ['ROI do período', percent(metrics?.roi)],
    ['Drawdown atual', money(metrics?.drawdown, balanceCurrency)],
    ['Drawdown máximo', money(metrics?.max_drawdown, balanceCurrency)],
    ['Contratos em andamento', number(metrics?.open_positions, 0)],
    ['Expectativa por operação', money(metrics?.expectancy, currency)],
  ]
  return <>
    <PageTitle title={terminal ? 'Terminal' : 'Visão geral'}
      description={terminal ? 'Acompanhe o mercado e controle uma sessão do motor.' : 'Acompanhe mercado, execução e resultados por ambiente.'}
      actions={<Badge tone={scope.mode === 'BROKER_DEMO' ? 'blue' : 'lime'} dot>{modeNames[scope.mode]}</Badge>} />
    <section className="panel scope-wrap" aria-label="Filtros do terminal">
      <ScopeFields scope={scope} update={value => { if (value.mode !== scope.mode || value.exchange !== scope.exchange) setTimeframe(300); update(value) }}
        strategies={strategies} readOnly />
    </section>
    {dashboard.isError && <Problem error={dashboard.error} retry={() => void dashboard.refetch()} />}
    {!terminal && scope.mode !== 'READ_ONLY' && <MetricStrip dashboard={dashboard.data} loading={dashboard.isPending} />}
    {bot.isError && <Problem error={bot.error} retry={() => void bot.refetch()} />}
    <div className={scope.mode === 'READ_ONLY' ? 'terminal-grid terminal-read-only' : 'terminal-grid'}>
      <MarketPanel market={market.data} error={market.error} retry={() => void market.refetch()} loading={market.isLoading}
        scope={scope} timeframe={timeframe} setTimeframe={setTimeframe} stream={stream} terminal={terminal} />
      {scope.mode !== 'READ_ONLY' && <ExecutionPanel bot={bot.data ?? dashboard.data?.bot} dashboard={dashboard.data} market={market.data} scope={scope}
        session={session} strategies={strategies} isLoading={bot.isPending} stream={stream} />}
    </div>
    {scope.mode !== 'READ_ONLY' && <div className="lower-grid">
      <section className="panel chart-section" aria-label={terminal ? 'Ordens e contratos' : 'Evolução do patrimônio'}>
        <div className="panel-heading">
          <div className="section-name"><span className="panel-icon">{terminal ? <CircleDot size={19} /> : <Activity size={19} />}</span>
            <div><h2>{terminal ? 'Ordens e contratos' : 'Evolução do patrimônio'}</h2><p>{exchangeNames[scope.exchange]} · {modeNames[scope.mode]}</p></div>
          </div>
          <Link className="quiet-link" to="/historico">Histórico <ChevronRight size={15} aria-hidden="true" /></Link>
        </div>
        {terminal ? <DataTable rows={dashboard.data?.orders ?? []} title="Ordens recentes" empty="Nenhuma ordem neste escopo" columns={[
          { key: 'instrument', label: 'Ativo', value: row => valueText(row.instrument) },
          { key: 'status', label: 'Estado', value: row => <Status state={String(row.status ?? 'UNKNOWN')} /> },
          { key: 'amount', label: 'Valor', value: row => money(field(row, 'amount', 'stake', 'price') as string, String(row.currency ?? 'USD')), numeric: true },
        ]} /> : dashboard.data?.equity && dashboard.data.equity.length > 0
          ? <EquityGraph points={dashboard.data.equity} />
          : <Empty title="Sem evolução registrada" description="O gráfico acompanha os saldos registrados neste escopo." />}
      </section>
      <section className="panel recent-events">
        <div className="panel-heading">
          <div className="section-name"><span className="panel-icon"><Radio size={19} aria-hidden="true" /></span>
            <div><h2>Eventos recentes</h2><p>Atividade deste ambiente</p></div>
          </div>
          <Link className="quiet-link" to="/eventos">Ver eventos <ChevronRight size={15} aria-hidden="true" /></Link>
        </div>
        <Feed events={dashboard.data?.events?.slice(0, 4) ?? []} compact />
      </section>
    </div>}
    {!terminal && scope.mode !== 'READ_ONLY' && <section className="panel performance">
      <div className="panel-heading">
        <div className="section-name"><span className="panel-icon"><Gauge size={19} aria-hidden="true" /></span>
          <div><h2>Desempenho</h2><p>Métricas do período e saldos por ambiente</p></div>
        </div>
        <button type="button" aria-expanded={showAllMetrics} onClick={() => setShowAllMetrics(value => !value)}>
          {showAllMetrics ? 'Ocultar métricas' : 'Ver métricas'}
        </button>
      </div>
      {showAllMetrics && <>
        <dl className="metrics-detail">{metricDetail.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
        {dashboard.data?.period_results && ([
          ['daily', 'Resultado diário'], ['weekly', 'Resultado semanal'], ['monthly', 'Resultado mensal'],
        ] as const).map(([period, title]) => <details className="market-depth form-details" key={period}>
          <summary>{title}</summary>
          <DataTable rows={dashboard.data?.period_results?.[period] ?? []} title={title} empty="Sem resultados neste período"
            columns={[
              { key: 'period', label: 'Início', value: row => dateTime(scalar(row.start) as string) },
              { key: 'net', label: 'Resultado líquido', value: row => money(scalar(row.net_pnl) as string, String(row.currency ?? currency)), numeric: true },
              { key: 'trades', label: 'Operações', value: row => number(scalar(row.trade_count) as string, 0), numeric: true },
            ]} />
        </details>)}
        <DataTable rows={dashboard.data?.strategy_summaries ?? []} title="Resultados por estratégia" empty="Sem resultados por estratégia" columns={summaryColumns} />
        <DataTable rows={dashboard.data?.mode_summaries ?? []} title="Saldos por corretora e ambiente" empty="Sem saldos por ambiente"
          columns={[...summaryColumns.slice(0, 2), { key: 'balance', label: 'Saldo', value: row => money(scalar(row.balance) as string, String(row.currency ?? 'USD')), numeric: true }]} />
      </>}
      {(dashboard.data?.connections?.length ?? 0) > 0 && <div className="connection-strip">
        {dashboard.data?.connections.map(connection => <div key={connection.exchange}>
          <strong>{exchangeNames[connection.exchange]}</strong><Status state={connection.status} />
          <small>{relativeTime(connection.last_sync)}</small>
        </div>)}
      </div>}
    </section>}
  </>
}
