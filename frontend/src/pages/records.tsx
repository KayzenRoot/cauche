import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, Download, Filter, Pause, Play, Search } from 'lucide-react'
import { api, params, scopeParams } from '../api'
import type { StreamStatus } from '../api'
import { Badge, DataTable, Feed, Loading, PageTitle, Problem, Status } from '../components/common'
import type { Column } from '../components/common'
import { ScopeFields, defaultInstrument } from '../components/scope'
import { dateTime, field, localText, money, number, strategyName, valueText } from '../format'
import type { EventsPage, History, Scope, Strategy } from '../types'

const historyViews = [
  { id: 'orders', label: 'Ordens' },
  { id: 'fills', label: 'Execuções' },
  { id: 'positions', label: 'Posições' },
  { id: 'settlements', label: 'Liquidações' },
  { id: 'fees', label: 'Taxas' },
] as const
type HistoryView = (typeof historyViews)[number]['id']
const historyBase: Column[] = [
  { key: 'timestamp', label: 'Data e hora', value: row => dateTime(field(row, 'timestamp', 'created_at', 'settled_at', 'updated_at') as string) },
  { key: 'id', label: 'Referência', value: row => <code>{valueText(field(row, 'broker_id', 'external_id', 'id'))}</code> },
  { key: 'instrument', label: 'Ativo', value: row => valueText(row.instrument) },
  { key: 'exchange', label: 'Corretora', value: row => valueText(row.exchange) },
  { key: 'mode', label: 'Ambiente', value: row => <Badge tone="neutral">{valueText(row.mode)}</Badge> },
  { key: 'strategy', label: 'Estratégia', value: row => strategyName(field(row, 'strategy', 'strategy_id') as string) },
]
const historyColumns: Record<HistoryView, Column[]> = {
  orders: [
    { key: 'side', label: 'Lado', value: row => valueText(row.side) },
    { key: 'status', label: 'Estado', value: row => <Status state={String(row.status ?? row.state ?? 'UNKNOWN')} /> },
    { key: 'price', label: 'Preço', value: row => number(field(row, 'price', 'limit_price') as string, 5), numeric: true },
    { key: 'quantity', label: 'Quantidade', value: row => number(field(row, 'quantity', 'size') as string), numeric: true },
    { key: 'remaining', label: 'Restante', value: row => number(field(row, 'remaining_count', 'remaining_quantity') as string), numeric: true },
  ],
  fills: [
    { key: 'order', label: 'Ordem', value: row => <code>{valueText(row.order_id)}</code> },
    { key: 'quantity', label: 'Quantidade', value: row => number(field(row, 'quantity', 'size', 'count') as string), numeric: true },
    { key: 'price', label: 'Preço', value: row => number(field(row, 'price', 'fill_price') as string, 5), numeric: true },
    { key: 'fee', label: 'Taxa', value: row => money(field(row, 'fee', 'fee_amount') as string, String(row.currency ?? 'USD')), numeric: true },
  ],
  positions: [
    { key: 'side', label: 'Lado', value: row => valueText(row.side) },
    { key: 'quantity', label: 'Quantidade', value: row => number(field(row, 'quantity', 'size') as string), numeric: true },
    { key: 'pnl', label: 'Resultado não realizado', value: row => money(field(row, 'unrealized_pnl') as string, String(row.currency ?? 'USD')), numeric: true },
    { key: 'status', label: 'Estado', value: row => <Status state={String(row.status ?? row.state ?? 'UNKNOWN')} /> },
  ],
  settlements: [
    { key: 'payout', label: 'Pagamento', value: row => money(field(row, 'payout', 'revenue') as string, String(row.currency ?? 'USD')), numeric: true },
    { key: 'gross', label: 'Resultado bruto', value: row => money(field(row, 'gross_pnl') as string, String(row.currency ?? 'USD')), numeric: true },
    { key: 'net', label: 'Resultado líquido', value: row => money(field(row, 'net_pnl', 'realized_pnl', 'pnl') as string, String(row.currency ?? 'USD')), numeric: true },
  ],
  fees: [{ key: 'fee', label: 'Valor', value: row => money(field(row, 'amount', 'fee', 'fee_amount') as string, String(row.currency ?? 'USD')), numeric: true }],
}

export function HistoryPage({ scope, update, strategies, analytics = false }: {
  scope: Scope; update: (value: Scope) => void; strategies: Strategy[]; analytics?: boolean;
}) {
  const [view, setView] = useState<HistoryView>(analytics ? 'settlements' : 'orders')
  const effective = scope.mode === 'READ_ONLY' ? { ...scope, mode: 'REPLAY' as const, instrument: defaultInstrument(scope.exchange, 'REPLAY') } : scope
  const scoped = scopeParams(effective)
  const data = useQuery({
    queryKey: ['history', scoped],
    queryFn: ({ signal }) => api<History>('/history' + params(scoped), { signal }),
  })
  const label = historyViews.find(item => item.id === view)?.label ?? 'Ordens'
  const rows = data.data?.[view] ?? []
  return <>
    <PageTitle title={analytics ? 'Resultados e custos' : 'Histórico'}
      description="Ordens, execuções e resultados confirmados neste ambiente." />
    <section className="panel scope-wrap" aria-label="Filtros do histórico"><ScopeFields scope={effective} update={update} strategies={strategies} /></section>
    <section className="panel">
      <div className="panel-heading records-heading">
        <div className="segmented" aria-label="Registros disponíveis">
          {historyViews.map(item => <button type="button" key={item.id} onClick={() => setView(item.id)} aria-pressed={view === item.id}>{item.label}</button>)}
        </div>
        <div className="buttons">
          <a className="button" href={'/api/history/export' + params({ ...scoped, format: 'csv' })} download><Download size={15} aria-hidden="true" /> CSV</a>
          <a className="button" href={'/api/history/export' + params({ ...scoped, format: 'json' })} download><Download size={15} aria-hidden="true" /> JSON</a>
        </div>
      </div>
      {data.isPending ? <Loading /> : data.isError ? <Problem error={data.error} retry={() => void data.refetch()} />
        : <DataTable columns={[...historyBase, ...historyColumns[view]]} rows={rows} title={label} empty={'Sem registros de ' + label.toLowerCase()} />}
    </section>
  </>
}

const types = ['MARKET', 'STRATEGY', 'SIGNAL', 'ORDER', 'FILL', 'SETTLEMENT', 'RISK', 'API', 'AUTH', 'ADMIN', 'SYSTEM', 'ERROR']
const severities = ['INFO', 'WARNING', 'ERROR', 'CRITICAL']
const initialFilters = { exchange: '', mode: '', strategy: '', severity: '', type: '', q: '', start: '', end: '' }

export function LogsPage({ strategies, stream, audit = false }: {strategies: Strategy[]; stream: StreamStatus; audit?: boolean}) {
  const [draft, setDraft] = useState(initialFilters)
  const [filters, setFilters] = useState(initialFilters)
  const [paused, setPaused] = useState(false)
  const [cursors, setCursors] = useState<number[]>([0])
  const [snapshot, setSnapshot] = useState<EventsPage>()
  const cursor = cursors.at(-1) ?? 0
  const request = { ...filters, limit: 25, ...(cursor > 0 ? { before: cursor } : {}) }
  const data = useQuery({
    queryKey: [audit ? 'admin-audit' : paused || cursor > 0 ? 'events-page' : 'events-live', request],
    queryFn: ({ signal }) => api<EventsPage>((audit ? '/admin/audit' : '/events') + params(request), { signal }),
    enabled: !paused || cursor > 0,
    refetchInterval: !paused && cursor === 0 && stream !== 'online' ? 10000 : false,
  })
  const page = paused && cursor === 0 ? snapshot : data.data
  const next = page?.has_more ? page.before : null
  const change = (key: keyof typeof draft, value: string) => setDraft(previous => ({ ...previous, [key]: value }))
  return <>
    <PageTitle title={audit ? 'Auditoria' : 'Eventos'}
      description={audit ? 'Trilha de ações de segurança e administração.' : 'Histórico do motor, do mercado e das corretoras em UTC.'}
      actions={<>
        <Badge tone={paused ? 'amber' : stream === 'online' ? 'green' : 'neutral'} dot>
          {paused ? 'Pausado' : stream === 'online' ? 'Conectado' : 'Reconectando'}
        </Badge>
        <button type="button" onClick={() => {
          setSnapshot(paused ? undefined : data.data)
          setPaused(value => !value)
        }}>{paused ? <Play size={15} /> : <Pause size={15} />}{paused ? 'Retomar atualização' : 'Pausar atualização'}</button>
      </>} />
    <form className="panel scope-wrap log-filters" onSubmit={event => {
      event.preventDefault()
      setPaused(false)
      setSnapshot(undefined)
      setCursors([0])
      setFilters(draft)
    }}>
      <div className="scope-bar">
        <label><span><Search size={13} aria-hidden="true" /> Buscar eventos</span><input maxLength={100} value={draft.q} onChange={event => change('q', event.target.value)} placeholder="Código ou mensagem" /></label>
        {(!audit ? [
          ['exchange', 'Corretora', ['DERIV', 'KALSHI']],
          ['mode', 'Ambiente', ['REPLAY', 'PAPER', 'BROKER_DEMO']],
          ['type', 'Tipo', types],
          ['severity', 'Severidade', severities],
        ] as const : [['severity', 'Severidade', severities]] as const).map(([key, label, items]) =>
          <label key={key}><span>{label}</span><select value={draft[key]} onChange={event => change(key, event.target.value)}>
            <option value="">Todos</option>{items.map(item => <option key={item} value={item}>{localText(item)}</option>)}
          </select></label>)}
        {!audit && <label><span>Estratégia</span><select value={draft.strategy} onChange={event => change('strategy', event.target.value)}>
          <option value="">Todas</option>{strategies.map(item => <option key={item.id} value={item.id}>{strategyName(item.id, item.name)}</option>)}
        </select></label>}
        <label><span>Data inicial</span><input type="date" value={draft.start} max={draft.end || undefined} onChange={event => change('start', event.target.value)} /></label>
        <label><span>Data final</span><input type="date" value={draft.end} min={draft.start || undefined} onChange={event => change('end', event.target.value)} /></label>
        <button type="submit"><Filter size={15} aria-hidden="true" /> Filtrar</button>
      </div>
    </form>
    <section className="panel">
      <div className="panel-heading records-heading">
        <h2>{audit ? 'Registros de auditoria' : 'Registros operacionais'}</h2>
        {!audit && <div className="buttons">
          <a className="button" href={'/api/events/export' + params({ ...filters, format: 'csv' })} download><Download size={15} aria-hidden="true" /> CSV</a>
          <a className="button" href={'/api/events/export' + params({ ...filters, format: 'json' })} download><Download size={15} aria-hidden="true" /> JSON</a>
        </div>}
      </div>
      {data.isPending && !paused ? <Loading /> : data.isError && !paused ? <Problem error={data.error} retry={() => void data.refetch()} />
        : <Feed events={page?.items ?? []} />}
      <div className="pagination">
        <button type="button" onClick={() => setCursors(list => list.slice(0, -1))} disabled={cursors.length < 2}><ChevronLeft size={15} /> Anterior</button>
        <span>Página {cursors.length}</span>
        <button type="button" onClick={() => { if (next) setCursors(list => [...list, next]) }}
          disabled={!next || next === cursor}>Próxima <ChevronRight size={15} /></button>
      </div>
    </section>
  </>
}
