import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BarChart3, FlaskConical, Play } from 'lucide-react'
import { api } from '../api'
import { Badge, DataTable, Empty, Loading, PageTitle, Problem, Status } from '../components/common'
import { EquityGraph } from '../components/chart'
import { ParameterFields } from '../components/parameters'
import { ScopeFields } from '../components/scope'
import { dateTime, exchangeNames, field, marketSource, money, number, percent, scalar, strategyName, valueText } from '../format'
import type { Backtest, List, RecordData, Scalar, Scope, Session, Strategy } from '../types'

const metrics = [
  { key: 'net_pnl', label: 'Resultado líquido', format: money },
  { key: 'roi', label: 'ROI', format: percent },
  { key: 'win_rate', label: 'Taxa de acerto', format: percent },
  { key: 'trades', label: 'Operações', format: number },
  { key: 'profit_factor', label: 'Fator de lucro', format: number },
  { key: 'expectancy', label: 'Expectativa por operação', format: money },
  { key: 'max_drawdown', label: 'Drawdown máximo', format: percent },
  { key: 'max_loss_streak', label: 'Maior sequência de perdas', format: number },
  { key: 'return_volatility', label: 'Volatilidade dos retornos', format: percent },
  { key: 'brier_score', label: 'Brier Score', format: number },
  { key: 'fees', label: 'Taxas totais', format: money },
]

function resultMetrics(test?: Backtest): RecordData {
  const result = test?.result
  const metrics = result?.metrics
  return (metrics && typeof metrics === 'object' && !Array.isArray(metrics) ? metrics : result ?? {}) as RecordData
}

function resultValue(data: RecordData, key: string) {
  const aliases: Record<string, string[]> = {
    trades: ['trades', 'trade_count'], fees: ['fees', 'total_fees'],
    max_loss_streak: ['max_loss_streak', 'longest_loss_streak'],
    return_volatility: ['return_volatility', 'volatility'],
  }
  return field(data, ...(aliases[key] ?? [key])) as string | number | null
}

function equity(test?: Backtest) {
  const series = resultMetrics(test).equity
  if (!Array.isArray(series)) return []
  return series.flatMap((row: unknown) => {
    if (typeof row !== 'object' || row === null) return []
    const data = row as RecordData
    const time = field(data, 'time', 'timestamp')
    const value = field(data, 'value', 'equity', 'balance')
    return (typeof time === 'string' || typeof time === 'number') && (typeof value === 'string' || typeof value === 'number')
      ? [{ time, value }] : []
  })
}

function recordData(value: unknown): RecordData {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RecordData : {}
}

function validationRows(test: Backtest) {
  const partitions = recordData(test.result?.temporal_partitions)
  const rows: RecordData[] = [
    ['train', 'Treino'], ['validation', 'Validação'], ['test', 'Teste fora da amostra'],
  ].filter(([key]) => key in partitions).map(([key, label]) => ({
    label, ...recordData(partitions[key]),
  }))
  const sensitivity = recordData(test.result?.slippage_sensitivity)
  if (sensitivity.metrics) rows.push({ label: 'Sensibilidade a slippage', metrics: sensitivity.metrics })
  return rows
}


function researchRows(value: unknown): RecordData[] {
  return Array.isArray(value) ? value.map(recordData) : []
}

function ResearchEvidence({ result }: { result: Backtest }) {
  const research = recordData(result.result?.research)
  const validation = recordData(research.walk_forward)
  const holdout = recordData(validation.holdout)
  const windows = researchRows(validation.windows).map((window, index) => ({ ...window, label: 'Janela ' + (index + 1) }))
  const partitions = Object.keys(holdout).length ? [...windows, { ...holdout, label: 'Teste final reservado' }] : windows
  const calibratedModels = researchRows(recordData(holdout.calibration).models)
  const regimes = researchRows(research.regimes)
  const execution = recordData(result.result?.execution)
  const names: Record<string, string> = {
    CURRENT: 'Estratégia atual', REGIME_FILTERED: 'Filtro de regime', MARKET_MIDPOINT: 'Preço médio de mercado',
    TRAIN_BASE_RATE: 'Taxa de acerto no treino', LOGISTIC_REGRESSION: 'Regressão logística', SHALLOW_TREE: 'Árvore rasa',
  }
  if (!Object.keys(research).length && !Object.keys(execution).length) return null
  return <>
    {Object.keys(research).length > 0 && <details className="form-body form-details">
      <summary>Validação cronológica e modelos em sombra</summary>
      <p className="hint">Evidência do replay. Os resultados em sombra não promovem estratégias automaticamente.</p>
      <DataTable title="Janelas cronológicas do backtest" empty="Dados cronológicos insuficientes" rows={partitions} columns={[
        { key: 'label', label: 'Janela', value: row => valueText(row.label) },
        { key: 'candidate', label: 'Selecionada na validação', value: row => names[String(row.selected_candidate)] ?? valueText(row.selected_candidate) },
        { key: 'count', label: 'Amostras de teste', numeric: true, value: row => number(scalar(recordData(row.test).observations) as string, 0) },
        { key: 'trades', label: 'Operações', numeric: true, value: row => number(resultValue(recordData(row.test_metrics), 'trades'), 0) },
        { key: 'pnl', label: 'Líquido', numeric: true, value: row => money(resultValue(recordData(row.test_metrics), 'net_pnl')) },
        { key: 'drawdown', label: 'Queda máxima', numeric: true, value: row => percent(resultValue(recordData(row.test_metrics), 'max_drawdown')) },
      ]} />
      {Object.keys(holdout).length > 0 && <DataTable title="Estratégias em sombra no teste final" empty="Nenhuma candidata no teste final"
        rows={Object.entries(recordData(holdout.shadow_candidates)).map(([name, metrics]) => ({ name, metrics }))} columns={[
          { key: 'name', label: 'Estratégia', value: row => names[String(row.name)] ?? valueText(row.name) },
          { key: 'trades', label: 'Operações', numeric: true, value: row => number(resultValue(recordData(row.metrics), 'trades'), 0) },
          { key: 'pnl', label: 'Líquido', numeric: true, value: row => money(resultValue(recordData(row.metrics), 'net_pnl')) },
          { key: 'drawdown', label: 'Queda máxima', numeric: true, value: row => percent(resultValue(recordData(row.metrics), 'max_drawdown')) },
        ]} />}
      {Object.keys(holdout).length > 0 && <DataTable title="Calibração no teste final" empty="Amostras insuficientes para comparar modelos" rows={calibratedModels} columns={[
        { key: 'model', label: 'Modelo', value: row => names[String(row.model)] ?? valueText(row.model) },
        { key: 'n', label: 'Desfechos', numeric: true, value: row => number(scalar(recordData(row.evaluation).observations) as string, 0) },
        { key: 'brier', label: 'Brier score', numeric: true, value: row => number(scalar(recordData(row.evaluation).brier_score) as string, 4) },
        { key: 'bins', label: 'Calibração', value: row => <details><summary>Ver faixas</summary><ul className="research-citations">
          {researchRows(recordData(row.evaluation).calibration_bins).map(bin => <li key={String(bin.lower)}>
            {percent(scalar(bin.lower) as string)}–{percent(scalar(bin.upper) as string)} · Prevista {percent(scalar(bin.mean_probability) as string)} · Observada {percent(scalar(bin.observed_rate) as string)}
          </li>)}
        </ul></details> },
      ]} />}
      {regimes.length > 0 && <DataTable title="Regimes e sessões do replay" empty="Nenhum regime observado" rows={regimes} columns={[
        { key: 'regime', label: 'Regime', value: row => valueText(row.regime) },
        { key: 'session', label: 'Sessão', value: row => valueText(row.session) },
        { key: 'trades', label: 'Operações', numeric: true, value: row => number(resultValue(row, 'trades'), 0) },
        { key: 'pnl', label: 'Líquido', numeric: true, value: row => money(resultValue(row, 'net_pnl')) },
      ]} />}
    </details>}
    {Object.keys(execution).length > 0 && <details className="form-body form-details">
      <summary>Qualidade da execução simulada</summary>
      <dl className="metrics-detail">
        <div><dt>Ordens avaliadas</dt><dd>{number(scalar(execution.submitted) as string, 0)}</dd></div>
        <div><dt>Preenchidas</dt><dd>{number(scalar(execution.filled) as string, 0)}</dd></div>
        <div><dt>Parciais</dt><dd>{number(scalar(execution.partial) as string, 0)}</dd></div>
        <div><dt>Latência média (s)</dt><dd>{number(scalar(execution.average_fill_latency_seconds) as string, 1)}</dd></div>
        <div><dt>Diferença média de preço</dt><dd>{money(scalar(execution.average_price_difference) as string)}</dd></div>
      </dl>
      <DataTable title="Desfechos de execução" empty="Nenhuma ordem simulada" rows={Object.entries(recordData(execution.outcomes)).map(([status, count]) => ({ status, count }))}
        columns={[
          { key: 'status', label: 'Desfecho', value: row => valueText(row.status) },
          { key: 'count', label: 'Ocorrências', numeric: true, value: row => number(scalar(row.count) as string, 0) },
        ]} />
    </details>}
  </>
}
export function BacktestsPage({ session, strategies }: {session: Session; strategies: Strategy[]}) {
  const client = useQueryClient()
  const [scope, setScope] = useState<Scope>({ exchange: 'DERIV', mode: 'REPLAY', strategy: '', instrument: 'EUR/USD', start: '', end: '' })
  const [balance, setBalance] = useState('1000')
  const [costs, setCosts] = useState<Record<string, Scalar>>({})
  const [risk, setRisk] = useState<Record<string, Scalar>>({})
  const [parameterDraft, setParameterDraft] = useState<Record<string, Scalar>>({})
  const [resultId, setResultId] = useState<string | number>()
  const [comparisonId, setComparisonId] = useState('')
  const strategy = strategies.find(item => item.id === scope.strategy)
  const history = useQuery({
    queryKey: ['backtests'],
    queryFn: ({ signal }) => api<List<Backtest>>('/backtests', { signal }),
  })
  const run = useMutation({
    mutationFn: () => api<Backtest>('/backtests', { method: 'POST', session, body: {
      exchange: scope.exchange, strategy: scope.strategy, instrument: scope.instrument,
      ...(scope.start ? { start: scope.start } : {}), ...(scope.end ? { end: scope.end } : {}),
      initial_balance: balance, parameters: { ...strategy?.parameters, ...parameterDraft }, costs, ...(Object.keys(risk).length ? { risk } : {}),
    } }),
    onSuccess: result => {
      setResultId(result.id)
      client.setQueryData<List<Backtest>>(['backtests'], previous => ({ items: [result, ...(previous?.items ?? []).filter(item => item.id !== result.id)] }))
      void client.invalidateQueries({ queryKey: ['backtests'] })
    },
  })
  const result = history.data?.items.find(item => item.id === resultId) ?? history.data?.items[0]
  const comparison = history.data?.items.find(item => String(item.id) === comparisonId && item.exchange === result?.exchange && item.id !== result?.id)
  const values = resultMetrics(result)
  const chart = equity(result)
  return <>
    <PageTitle title="Backtests" description="Reproduza estratégias com dados identificados e compare execuções do mesmo produto." />
    <div className="split-grid backtest-grid">
      <form className="panel" onSubmit={event => { event.preventDefault(); run.mutate() }}>
        <div className="panel-heading">
          <div className="section-name"><span className="panel-icon"><FlaskConical size={19} aria-hidden="true" /></span>
            <div><h2>Nova simulação</h2><p>Replay histórico</p></div>
          </div>
        </div>
        <div className="form-body">
          <ScopeFields scope={scope} strategies={strategies} backtest update={next => {
            if (scope.strategy !== next.strategy) setParameterDraft({})
            setScope(next)
          }} />
          <label><span>Saldo virtual inicial (USD)</span><input required type="number" min="1" max="1000000" step="0.01" value={balance} onChange={event => setBalance(event.target.value)} /></label>
          {strategy && <details className="form-details">
            <summary>Parâmetros da estratégia</summary>
            <ParameterFields values={{ ...strategy.parameters, ...parameterDraft }} definitions={strategy.parameter_schema} update={(key, value) => setParameterDraft(previous => ({ ...previous, [key]: value }))} />
          </details>}
          <details className="form-details">
            <summary>Custos e gerenciamento de risco</summary>
            <ParameterFields values={{ fee_rate: '0', slippage: '0', ...costs }}
              definitions={{ fee_rate: { type: 'decimal', minimum: '0', maximum: '0.5' }, slippage: { type: 'decimal', minimum: '0', maximum: '0.5' } }}
              update={(key, value) => setCosts(previous => ({ ...previous, [key]: value }))} />
            <label><span>Exposição máxima por ordem (USD)</span>
              <input type="number" step="0.01" min="0.01" placeholder="Limite padrão" value={String(risk.max_order_exposure ?? '')}
                onChange={event => setRisk(event.target.value ? { max_order_exposure: event.target.value } : {})} />
            </label>
          </details>
          {run.isError && <Problem error={run.error} />}
          <button className="button-primary" disabled={run.isPending || !strategy} type="submit"><Play size={16} aria-hidden="true" /> {run.isPending ? 'Executando…' : 'Executar backtest'}</button>
        </div>
      </form>
      <section className="panel">
        <div className="panel-heading">
          <div className="section-name"><span className="panel-icon"><BarChart3 size={19} aria-hidden="true" /></span>
            <div><h2>Resultado</h2><p>{result ? marketSource(result.source) + ' · ' + dateTime(result.created_at) : 'Selecione uma execução'}</p></div>
          </div>
          {result && <Status state={result.status} />}
        </div>
        {history.isPending ? <Loading /> : history.isError ? <Problem error={history.error} retry={() => void history.refetch()} /> : !result
          ? <Empty title="Nenhum backtest registrado" description="Escolha uma estratégia e execute a primeira simulação." />
          : <>
            <div className="form-body">
              <div className="buttons"><Badge tone="lime">{exchangeNames[result.exchange]}</Badge><Badge>{strategyName(result.strategy)}</Badge><Badge>{result.instrument}</Badge></div>
              <label><span>Comparar execução</span><select value={comparison ? comparisonId : ''} onChange={event => setComparisonId(event.target.value)}>
                <option value="">Sem comparação</option>
                {history.data?.items.filter(item => item.exchange === result.exchange && item.id !== result.id).map(item =>
                  <option key={item.id} value={String(item.id)}>{strategyName(item.strategy)} · {dateTime(item.created_at)}</option>)}
              </select></label>
            </div>
            <DataTable title="Métricas do backtest" empty="Sem métricas" rows={metrics.map(item => ({
              label: item.label, current: item.format(resultValue(values, item.key)),
              prior: item.format(resultValue(resultMetrics(comparison), item.key)),
            }))} columns={[
              { key: 'label', label: 'Métrica', value: row => valueText(row.label) },
              { key: 'current', label: 'Selecionado', value: row => valueText(row.current), numeric: true },
              ...(comparison ? [{ key: 'prior', label: 'Comparativo', value: (row: RecordData) => valueText(row.prior), numeric: true }] : []),
            ]} />
            {chart.length > 0 && <EquityGraph points={chart} />}
            <ResearchEvidence result={result} />
            {validationRows(result).length > 0 && <details className="form-body form-details">
              <summary>Validação temporal e sensibilidade</summary>
              <DataTable title="Partições e sensibilidade" empty="Sem resultados temporais" rows={validationRows(result)}
                columns={[
                  { key: 'part', label: 'Partição', value: row => valueText(row.label) },
                  { key: 'count', label: 'Observações', value: row => number(scalar(row.observations) as string, 0), numeric: true },
                  { key: 'net', label: 'Resultado líquido', value: row => money(resultValue(recordData(row.metrics), 'net_pnl')), numeric: true },
                  { key: 'roi', label: 'ROI', value: row => percent(resultValue(recordData(row.metrics), 'roi')), numeric: true },
                ]} />
            </details>}
          </>}
      </section>
    </div>
    <section className="panel">
      <div className="panel-heading"><h2>Execuções registradas</h2></div>
      <DataTable title="Histórico de backtests" empty="Sem execuções" rows={(history.data?.items ?? []).map(item => ({ ...item }))}
        columns={[
          { key: 'strategy', label: 'Estratégia', value: row => strategyName(String(row.strategy)) },
          { key: 'exchange', label: 'Corretora', value: row => valueText(row.exchange) },
          { key: 'time', label: 'Executado em', value: row => dateTime(scalar(row.created_at) as string) },
          { key: 'result', label: 'Resultado', value: row => <button type="button" onClick={() => { setResultId(typeof row.id === 'number' ? row.id : String(row.id)); setComparisonId('') }}>Abrir resultado</button> },
        ]} />
    </section>
  </>
}
