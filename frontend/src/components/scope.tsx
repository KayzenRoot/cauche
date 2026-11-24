import { CalendarDays, Layers3, Search } from 'lucide-react'
import { useId, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, params } from '../api'
import { exchangeNames, fixtures, instrumentLabel, modeNames, strategyName } from '../format'
import type { Exchange, Instrument, List, MarketMode, Scope, Strategy } from '../types'
import { Problem } from './common'

export function defaultInstrument(exchange: Exchange, mode: MarketMode) {
  if (mode === 'BROKER_DEMO' || mode === 'READ_ONLY') return ''
  if (mode === 'PAPER' && exchange === 'DERIV') return 'FIXTURE:EUR/USD'
  return fixtures[exchange].id
}

function offlineInstruments(exchange: Exchange, mode: MarketMode): Instrument[] {
  if (mode !== 'REPLAY' && mode !== 'PAPER') return []
  if (exchange === 'KALSHI') return [{ id: fixtures.KALSHI.id, label: fixtures.KALSHI.label, source: 'FIXTURE' }]
  return ['EUR/USD', 'GBP/USD', 'USD/JPY'].map(currency => ({
    id: mode === 'PAPER' ? 'FIXTURE:' + currency : currency, label: currency + ' · fixture', source: 'FIXTURE',
  }))
}

export function ScopeFields({
  scope, update, strategies, date = true, readOnly = false, backtest = false,
}: {
  scope: Scope; update: (next: Scope) => void; strategies: Strategy[]; date?: boolean; readOnly?: boolean; backtest?: boolean;
}) {
  const inputListId = useId()
  const [category, setCategory] = useState('')
  const marketMode = scope.mode === 'READ_ONLY' ? 'PAPER' : scope.mode
  const discover = useMutation({
    mutationFn: (selection: { exchange: Exchange; mode: string; category: string }) =>
      api<List<Instrument>>('/market/instruments' + params({ ...selection, discover: true })),
  })
  const discovered = discover.variables?.exchange === scope.exchange && discover.variables?.mode === marketMode ? discover.data?.items ?? [] : []
  const instruments = useQuery({
    queryKey: ['instruments', scope.exchange, marketMode],
    queryFn: ({ signal }) => api<List<Instrument>>('/market/instruments' + params({ exchange: scope.exchange, mode: marketMode }), { signal }),
    staleTime: 60000,
    enabled: !backtest,
  })
  const scopeStrategies = strategies.filter(item => item.exchange === scope.exchange)
  const instrumentList = new Map<string, Instrument>()
  for (const item of [
    ...(!backtest ? instruments.data?.items ?? [] : []),
    ...offlineInstruments(scope.exchange, scope.mode),
    ...discovered,
  ]) {
    if (scope.mode !== 'READ_ONLY' || !item.source.toUpperCase().includes('FIXTURE')) instrumentList.set(item.id, item)
  }
  const changeExchange = (exchange: Exchange) => {
    const options = strategies.filter(item => item.exchange === exchange)
    const strategy = options.find(item => item.enabled)?.id ?? options[0]?.id ?? ''
    update({ ...scope, exchange, instrument: defaultInstrument(exchange, scope.mode), strategy: scope.strategy || backtest ? strategy : '' })
  }
  return <><div className="scope-bar">
    {!backtest && <label><span><Layers3 size={13} aria-hidden="true" /> Modo de execução</span>
      <select value={scope.mode} onChange={event => {
        const mode = event.target.value as MarketMode
        update({ ...scope, mode, instrument: defaultInstrument(scope.exchange, mode) })
      }}>
        {(readOnly ? ['REPLAY', 'PAPER', 'BROKER_DEMO', 'READ_ONLY'] as const : ['REPLAY', 'PAPER', 'BROKER_DEMO'] as const)
          .map(mode => <option key={mode} value={mode}>{modeNames[mode]}</option>)}
      </select>
    </label>}
    <label><span>Corretora</span>
      <select value={scope.exchange} onChange={event => changeExchange(event.target.value as Exchange)}>
        <option value="DERIV">{exchangeNames.DERIV}</option>
        <option value="KALSHI">{exchangeNames.KALSHI}</option>
      </select>
    </label>
    <label><span>Ativo ou contrato</span>
      {scope.mode === 'REPLAY' ? <select value={scope.instrument} onChange={event => update({ ...scope, instrument: event.target.value })}>
        {scope.instrument && !instrumentList.has(scope.instrument) && <option value={scope.instrument}>{instrumentLabel(scope.instrument)}</option>}
        {[...instrumentList.values()].map(item => <option value={item.id} key={item.id}>{item.label}</option>)}
      </select> : <>
        <input list={inputListId} value={scope.instrument} autoComplete="off" maxLength={100}
          placeholder={scope.exchange === 'DERIV' ? 'Ex. frxEURUSD' : 'Ticker da Kalshi'}
          onChange={event => update({ ...scope, instrument: event.target.value.trim() })} />
        <datalist id={inputListId}>
          {[...instrumentList.values()].map(item => <option value={item.id} key={item.id}>{item.label}</option>)}
        </datalist>
      </>}
    </label>
    <label><span>Estratégia</span>
      <select value={scope.strategy} required={backtest} onChange={event => update({ ...scope, strategy: event.target.value })}>
        <option value="">{backtest ? 'Selecione' : 'Todas as estratégias'}</option>
        {scopeStrategies.map(item => <option key={item.id} value={item.id}>{strategyName(item.id, item.name)}{!item.enabled && !backtest ? ' · inativa' : ''}</option>)}
      </select>
    </label>
    {date && <>
      <label className="date-field"><span><CalendarDays size={13} aria-hidden="true" /> Data inicial</span>
        <input type="date" value={scope.start} max={scope.end || undefined} onChange={event => update({ ...scope, start: event.target.value })} />
      </label>
      <label className="date-field"><span>Data final</span>
        <input type="date" value={scope.end} min={scope.start || undefined} onChange={event => update({ ...scope, end: event.target.value })} />
      </label>
    </>}
  </div>
  {scope.mode !== 'REPLAY' && !backtest && <div className="market-discovery">
    {scope.exchange === 'KALSHI' && <label><span>Categoria (opcional)</span>
      <input value={category} maxLength={100} placeholder="Ex. Weather" onChange={event => setCategory(event.target.value)} />
    </label>}
    <button type="button" disabled={discover.isPending} onClick={() => discover.mutate({
      exchange: scope.exchange, mode: marketMode, category: scope.exchange === 'KALSHI' ? category.trim() : '',
    })}><Search size={15} aria-hidden="true" /> {discover.isPending ? 'Buscando mercados…' : 'Buscar mercados oficiais'}</button>
    {discover.isSuccess && discovered.length > 0 && <p className="hint" role="status">{discovered.length} mercados disponíveis na lista de ativos.</p>}
    {discover.isSuccess && discover.variables?.exchange === scope.exchange && discovered.length === 0 && <p className="hint" role="status">Nenhum mercado encontrado para esta seleção.</p>}
    {discover.isError && discover.variables?.exchange === scope.exchange && <Problem error={discover.error} />}
  </div>}</>
}
