import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, BookOpen, Download, FlaskConical, Play } from 'lucide-react'
import { api } from '../api'
import { Badge, DataTable, Empty, Loading, PageTitle, Problem } from '../components/common'
import { dateTime, money, number, percent, valueText } from '../format'
import { analyze, initialLimits, parseDataset, sampleDataset } from '../research'
import type { Dataset, Metrics, Report, ResearchLimits } from '../research'
import type { RecordData, Session } from '../types'

type Input = { dataset: Dataset; label: string; synthetic: boolean }
type Journal = { items: RecordData[]; total: number; offset: number; limit: number; dataset?: unknown }
type ModelStatus = { model: string; configured: boolean }
type Source = { id: string; url: string; published_at: string | null }
type Inspection = {
  ticker: string
  decision_status: 'REVIEW_REQUIRED'
  observed_at: string
  rules: { primary: string; secondary: string; closes_at: string | null }
  model: string | null
  model_status?: 'EXTRACTED' | 'NOT_CONFIGURED' | 'NO_RULES' | 'UNAVAILABLE'
  journal_id?: string | number
  book?: {
    status: 'OBSERVED' | 'UNAVAILABLE'
    observed_at?: string
    yes_ask?: { price: string | number; quantity: string | number } | null
    no_ask?: { price: string | number; quantity: string | number } | null
  }
  sources: Source[]
  extraction: {
    event: string
    deadline: string | null
    resolution_source: string | null
    ambiguous: boolean
    facts: { source_id: string; quote: string; claim: string }[]
  } | null
}
type SourceDraft = { url: string; published: string; text: string }
const freeProfile = import.meta.env.MODE === 'cauche-free'
const MAX_FILE_BYTES = 2_000_000
const journalLimit = 50
const reasons = {
  EDGE: 'Vantagem após custos',
  LOW_EDGE: 'Margem abaixo do mínimo',
  EVENT_LIMIT: 'Limite do evento',
  PORTFOLIO_LIMIT: 'Limite da carteira',
  STRATEGY_LIMIT: 'Limite da estratégia',
  DAILY_LOSS: 'Limite de perda do dia',
}

const amount = (value: unknown, currency: string) => money(typeof value === 'number' ? value : undefined, currency)
const percentValue = (value: unknown) => percent(typeof value === 'number' ? value : undefined)
const numberValue = (value: unknown, digits = 0) => number(typeof value === 'number' ? value : undefined, digits)
const iso = (value: number) => Number.isFinite(value) ? new Date(value).toISOString() : undefined
const record = (value: unknown): RecordData =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RecordData : {}
const string = (value: unknown) => typeof value === 'string' ? value : undefined
const secureUrl = (value: string) => {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password ? url.toString() : undefined
  } catch {
    return undefined
  }
}

function downloadJson(filename: string, data: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function summaries(metrics: Metrics, currency: string) {
  return [
    { label: 'Resultado líquido', value: amount(metrics.net, currency), helper: 'Após custos do cenário' },
    { label: 'Queda máxima', value: amount(metrics.drawdown, currency), helper: 'Do pico ao menor resultado acumulado' },
    { label: 'Operações liquidadas', value: String(metrics.closed), helper: metrics.samples + ' decisões avaliadas' },
    { label: 'Brier score', value: numberValue(metrics.brier, 4), helper: 'Quanto menor, melhor' },
  ]
}

function ForecastReport({ report, currency }: {report: Report; currency: string}) {
  const [decisionPage, setDecisionPage] = useState(0)
  const [filter, setFilter] = useState<'all' | 'blocked' | 'accepted'>('all')
  const latest = report.windows.at(-1)
  const candidates = report.decisions.filter(item => filter === 'all' || (item.reason === 'EDGE') === (filter === 'accepted'))
  const pageSize = 25
  const list = candidates.slice(decisionPage * pageSize, (decisionPage + 1) * pageSize).map(item => ({
    ...item.row, calibrated: item.probability, expected: item.expected, executable: item.executableCost,
    worst: item.maxLoss, simulatedNet: item.net, signal: item.reason === 'EDGE', rationale: reasons[item.reason],
  }))
  const holdout = latest?.candidate
  return <>
    {holdout && <section aria-label="Métricas do teste final" className="metric-strip">
      {summaries(holdout, currency).map(metric => <div className="panel metric" key={metric.label}>
        <div className="metric-heading"><h2>{metric.label}</h2></div><strong>{metric.value}</strong><small>{metric.helper}</small>
      </div>)}
    </section>}
    <section className="panel">
      <div className="panel-heading"><div><h2>Comparação em sombra</h2><p>A referência e a regressão logística calibrada avaliam as mesmas decisões.</p></div></div>
      <DataTable title="Comparação dos modelos" empty="Sem previsões liquidadas" rows={[
        { label: 'Referência · todas as janelas', ...report.baseline },
        { label: 'Candidata · todas as janelas', ...report.candidate },
        ...(latest ? [{ label: 'Referência · teste final', ...latest.baseline }, { label: 'Candidata · teste final', ...latest.candidate }] : []),
        ...(report.marketSamples ? [
          { label: 'Mercado · amostra pareada', samples: report.marketSamples, brier: report.marketBrier },
          { label: 'Candidata · amostra pareada', samples: report.marketSamples, brier: report.candidateMarketBrier },
        ] : []),
      ]} columns={[
        { key: 'label', label: 'Modelo', value: row => valueText(row.label) },
        { key: 'samples', label: 'Avaliadas', numeric: true, value: row => numberValue(row.samples) },
        { key: 'closed', label: 'Liquidadas', numeric: true, value: row => numberValue(row.closed) },
        { key: 'net', label: 'Resultado líquido', numeric: true, value: row => amount(row.net, currency) },
        { key: 'drawdown', label: 'Queda máxima', numeric: true, value: row => amount(row.drawdown, currency) },
        { key: 'brier', label: 'Brier', numeric: true, value: row => numberValue(row.brier, 4) },
      ]} />
    </section>
    <div className="split-grid research-comparison">
      <section className="panel">
        <div className="panel-heading"><div><h2>Janelas cronológicas</h2><p>Só liquidações conhecidas antes da janela entram no treino.</p></div></div>
        <DataTable title="Janelas cronológicas" empty="Sem janelas" rows={report.windows.map((row, index) => ({
          label: row.holdout ? 'Teste final' : 'Janela ' + (index + 1), start: iso(row.start), train: row.train,
          fitted: row.fitted, ...row.candidate,
        }))} columns={[
          { key: 'label', label: 'Janela', value: row => <>{valueText(row.label)}<br /><small>{dateTime(string(row.start))}</small></> },
          { key: 'train', label: 'Treino', numeric: true, value: row => numberValue(row.train) },
          { key: 'fitted', label: 'Modelo', value: row => row.fitted ? 'Calibrado' : 'Referência' },
          { key: 'samples', label: 'Avaliadas', numeric: true, value: row => numberValue(row.samples) },
          { key: 'net', label: 'Líquido', numeric: true, value: row => amount(row.net, currency) },
          { key: 'drawdown', label: 'Queda', numeric: true, value: row => amount(row.drawdown, currency) },
        ]} />
      </section>
      <section className="panel">
        <div className="panel-heading"><div><h2>Calibração da candidata</h2><p>Compare probabilidade média e frequência observada.</p></div></div>
        <DataTable title="Calibração da candidata" empty="Sem desfechos observados" rows={report.candidate.reliability} columns={[
          { key: 'bucket', label: 'Faixa', value: row => valueText(row.bucket) },
          { key: 'count', label: 'Desfechos', numeric: true, value: row => numberValue(row.count) },
          { key: 'predicted', label: 'Prevista', numeric: true, value: row => percentValue(row.predicted) },
          { key: 'observed', label: 'Observada', numeric: true, value: row => percentValue(row.observed) },
        ]} />
      </section>
    </div>
    <section className="panel">
      <div className="panel-heading"><div><h2>Estratégias por regime</h2><p>Resultados descritivos da candidata. Amostras pequenas exigem mais observações antes de decidir.</p></div></div>
      <DataTable title="Estratégias por regime" empty="Sem grupos" rows={report.groups.map(({ name, metrics }) => ({ name, ...metrics }))} columns={[
        { key: 'name', label: 'Corretora, ativo, estratégia e regime', value: row => valueText(row.name) },
        { key: 'samples', label: 'Avaliadas', numeric: true, value: row => numberValue(row.samples) },
        { key: 'closed', label: 'Liquidadas', numeric: true, value: row => numberValue(row.closed) },
        { key: 'net', label: 'Líquido', numeric: true, value: row => amount(row.net, currency) },
        { key: 'drawdown', label: 'Queda', numeric: true, value: row => amount(row.drawdown, currency) },
        { key: 'brier', label: 'Brier', numeric: true, value: row => numberValue(row.brier, 4) },
      ]} />
    </section>
    <section className="panel">
      <div className="panel-heading"><div><h2>Qualidade de execução</h2><p>O cenário usa a execução informada no arquivo ou simula preenchimento integral ao custo estimado.</p></div></div>
      <dl className="metrics-detail">
        <div><dt>Sinais com execução informada</dt><dd>{report.candidate.observedExecution}</dd></div>
        <div><dt>Rejeições registradas</dt><dd>{report.candidate.rejected}</dd></div>
        <div><dt>Preenchimentos parciais</dt><dd>{report.candidate.partial}</dd></div>
        <div><dt>Latência média observada</dt><dd>{report.candidate.meanLatency === null ? '—' : number(report.candidate.meanLatency, 0) + ' ms'}</dd></div>
        <div><dt>Diferença de preço média</dt><dd>{amount(report.candidate.meanSlippage, currency)}</dd></div>
      </dl>
    </section>
    <section className="panel">
      <div className="panel-heading records-heading"><div><h2>Decisões da candidata</h2><p>Inclui entradas do cenário e motivos para ficar de fora.</p></div>
        <div className="segmented" role="group" aria-label="Filtrar decisões">
          {([['all', 'Todas'], ['accepted', 'Com sinal'], ['blocked', 'Sem sinal']] as const).map(([value, label]) =>
            <button key={value} type="button" aria-pressed={filter === value} onClick={() => { setFilter(value); setDecisionPage(0) }}>{label}</button>)}
        </div>
      </div>
      <DataTable title="Decisões e evidências" empty="Sem decisões neste filtro" rows={list} columns={[
        { key: 'instrument', label: 'Instrumento', value: row => <>{valueText(row.instrument)}<br /><small>{dateTime(string(row.decision_at))}</small></> },
        { key: 'rationale', label: 'Decisão', value: row => <><Badge tone={row.signal ? 'green' : 'amber'}>{row.signal ? 'Com sinal' : 'Sem sinal'}</Badge><br />{valueText(row.rationale)}</> },
        { key: 'calibrated', label: 'Prob. estimada', numeric: true, value: row => percentValue(row.calibrated) },
        { key: 'cost', label: 'Preço executável', numeric: true, value: row => amount(row.cost, currency) },
        { key: 'expected', label: 'Valor esperado líquido', numeric: true, value: row => amount(row.expected, currency) },
        { key: 'worst', label: 'Perda máxima estimada', numeric: true, value: row => amount(row.worst, currency) },
        { key: 'simulatedNet', label: 'Resultado', numeric: true, value: row => amount(row.simulatedNet, currency) },
        { key: 'source', label: 'Evidência', value: row => <details>
          <summary>Ver registro</summary><dl className="details-list">
            <div><dt>Fonte</dt><dd>{valueText(row.source)}</dd></div>
            <div><dt>Observada</dt><dd>{dateTime(string(row.observed_at))}</dd></div>
            <div><dt>Liquidação</dt><dd>{dateTime(string(row.settled_at))}</dd></div>
            <div><dt>Pagamento bruto</dt><dd>{amount(row.payout, currency)}</dd></div>
          </dl>
        </details> },
      ]} />
      {candidates.length > pageSize && <div className="pagination">
        <button type="button" disabled={decisionPage === 0} onClick={() => setDecisionPage(page => page - 1)}>Anterior</button>
        <span>Página {decisionPage + 1} de {Math.ceil(candidates.length / pageSize)}</span>
        <button type="button" disabled={(decisionPage + 1) * pageSize >= candidates.length} onClick={() => setDecisionPage(page => page + 1)}>Próxima</button>
      </div>}
    </section>
  </>
}

function QuantitativeStudy() {
  const [input, setInput] = useState<Input>()
  const [error, setError] = useState<unknown>()
  const [limits, setLimits] = useState<Record<keyof ResearchLimits, string>>({
    minimumEdge: String(initialLimits.minimumEdge), exposure: String(initialLimits.exposure),
    eventExposure: String(initialLimits.eventExposure), strategyExposure: String(initialLimits.strategyExposure), dailyLoss: String(initialLimits.dailyLoss),
    additionalSlippage: String(initialLimits.additionalSlippage),
  })
  const [output, setOutput] = useState<{ report: Report; currency: string; label: string; synthetic: boolean; run: number }>()
  const [offset, setOffset] = useState<number | null>(null)
  const journal = useQuery({
    queryKey: ['research-evidence', offset],
    queryFn: ({ signal }) => api<Journal>('/research/evidence?limit=' + journalLimit + '&offset=' + (offset ?? 0), { signal }),
    enabled: offset !== null,
    retry: false,
  })
  const load = async (file: File) => {
    try {
      setInput(undefined)
      setOutput(undefined)
      setError(undefined)
      if (file.size > MAX_FILE_BYTES) throw new Error('O arquivo deve ter até 2 MB.')
      let data: unknown
      try { data = JSON.parse(await file.text()) }
      catch { throw new Error('O arquivo não contém JSON válido.') }
      setInput({ dataset: parseDataset(data), label: file.name, synthetic: false })
    } catch (failure) {
      setError(failure)
    }
  }
  const evaluate = (event: FormEvent) => {
    event.preventDefault()
    if (!input) return
    try {
      const values = {
        minimumEdge: Number(limits.minimumEdge), exposure: Number(limits.exposure), eventExposure: Number(limits.eventExposure),
        strategyExposure: Number(limits.strategyExposure), dailyLoss: Number(limits.dailyLoss), additionalSlippage: Number(limits.additionalSlippage),
      }
      const report = analyze(input.dataset, values)
      setOutput(previous => ({ report, currency: input.dataset.currency, label: input.label, synthetic: input.synthetic, run: (previous?.run ?? 0) + 1 }))
      setError(undefined)
    } catch (failure) {
      setError(failure)
    }
  }
  const fields: [keyof ResearchLimits, string][] = [
    ['minimumEdge', 'Valor esperado mínimo'], ['exposure', 'Exposição da carteira'], ['eventExposure', 'Exposição por evento'],
    ['strategyExposure', 'Exposição por estratégia'], ['dailyLoss', 'Limite diário de perdas'], ['additionalSlippage', 'Custo adicional por derrapagem'],
  ]
  return <>
    <form className="panel" onSubmit={evaluate}>
      <div className="panel-heading"><div className="section-name"><span className="panel-icon"><FlaskConical size={18} aria-hidden="true" /></span>
        <div><h2>Validação de estratégias</h2><p>Compare previsões em replay, sem executar ordens.</p></div>
      </div>{input && <Badge tone={input.synthetic ? 'amber' : 'blue'}>{input.synthetic ? 'Dados sintéticos' : 'Dados importados'}</Badge>}</div>
      <div className="form-body">
        <div className="buttons">
          <button type="button" onClick={() => {
            setInput({ dataset: sampleDataset(), label: 'Amostra sintética', synthetic: true })
            setOutput(undefined); setError(undefined)
          }}>Carregar amostra sintética</button>
          <button type="button" onClick={() => downloadJson('cauche-evidencias-amostra.json', sampleDataset())}><Download size={15} aria-hidden="true" /> Baixar exemplo de JSON</button>
          <button type="button" onClick={() => {
            if (offset === 0) void journal.refetch()
            else setOffset(0)
          }}><Activity size={15} aria-hidden="true" /> Consultar diário</button>
        </div>
        <label><span>Importar evidências em JSON</span><input type="file" accept=".json,application/json" onChange={event => {
          const file = event.currentTarget.files?.[0]
          if (file) void load(file)
          event.currentTarget.value = ''
        }} /></label>
        {input && <p role="status" className="hint">{input.label} · {input.dataset.records.length} observações · {input.dataset.currency}</p>}
        <details className="form-details">
          <summary>Limites e custos do cenário ({input?.dataset.currency ?? 'moeda da amostra'})</summary>
          <div className="parameter-grid">{fields.map(([key, label]) => <label key={key}><span>{label}</span>
            <input required type="number" min={key === 'minimumEdge' || key === 'additionalSlippage' ? '0' : '0.01'} max="10000000"
              step="0.01" value={limits[key]} onChange={event => setLimits(previous => ({ ...previous, [key]: event.target.value }))} />
          </label>)}</div>
        </details>
        {error instanceof Error && <Problem error={error} />}
        <button type="submit" className="button-primary" disabled={!input}><Play size={15} aria-hidden="true" /> Executar validação</button>
      </div>
    </form>
    {offset !== null && <section className="panel">
      <div className="panel-heading records-heading"><div><h2>Diário de evidências</h2><p>{freeProfile ? 'Consultas públicas da Kalshi registradas com horário, regras e livro de ofertas.' : 'Decisões observadas pelo motor, inclusive sinais descartados.'}</p></div>
        {journal.data && <div className="buttons">
          <button type="button" onClick={() => downloadJson('cauche-diario-' + offset + '.json', journal.data)}><Download size={15} aria-hidden="true" /> Exportar esta página</button>
          {journal.data.dataset !== undefined && <button type="button" onClick={() => {
            try {
              setInput({ dataset: parseDataset(journal.data?.dataset), label: 'Amostra do diário', synthetic: false })
              setOutput(undefined); setError(undefined)
            } catch (failure) { setError(failure) }
          }}>Carregar para validação</button>}
        </div>}
      </div>
      {journal.isPending ? <Loading /> : journal.isError ? <Problem error={journal.error} retry={() => void journal.refetch()} />
        : journal.data && <>
          <DataTable title="Evidências do motor" empty="Nenhuma decisão registrada" rows={journal.data.items} columns={[
            { key: 'instrument', label: 'Mercado', value: row => valueText(row.instrument) },
            { key: 'mode', label: 'Modo', value: row => valueText(row.mode) },
            { key: 'timestamp', label: 'Decisão em', value: row => dateTime(typeof row.timestamp === 'number' ? row.timestamp : string(row.timestamp)) },
            { key: 'reason', label: 'Motivo', value: row => valueText(row.reason) },
            { key: 'execution', label: 'Execução', value: row => valueText(record(row.execution).status) },
            { key: 'data', label: 'Registro', value: row => <details><summary>Ver detalhes</summary>
              <pre className="research-json">{JSON.stringify(row, null, 2)}</pre></details> },
          ]} />
          {journal.data.total > journalLimit && <div className="pagination">
            <button type="button" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - journalLimit))}>Anterior</button>
            <span>{Math.min(offset + 1, journal.data.total)}–{Math.min(offset + journalLimit, journal.data.total)} de {journal.data.total}</span>
            <button type="button" disabled={offset + journalLimit >= journal.data.total} onClick={() => setOffset(offset + journalLimit)}>Próxima</button>
          </div>}
        </>}
    </section>}
    {output && <>
      <section className="panel setting-banner" aria-label="Origem do resultado"><div><h2>Resultado do teste cronológico</h2>
        <p>{output.label} · {output.synthetic ? 'Amostra sintética' : 'Validação exploratória de dados importados'}</p></div>
        <button type="button" onClick={() => downloadJson('cauche-validacao.json', output)}><Download size={15} aria-hidden="true" /> Exportar resultado</button>
      </section>
      <ForecastReport key={output.run} report={output.report} currency={output.currency} />
    </>}
  </>
}

function SourceLink({ source }: { source: Source }) {
  const href = secureUrl(source.url)
  return <span>{href ? <a className="quiet-link research-source" href={href} target="_blank" rel="noreferrer">{source.url}</a> : source.url}
    {source.published_at && <> · {dateTime(source.published_at)}</>}</span>
}

function KalshiResearch({ session }: {session: Session}) {
  const client = useQueryClient()
  const [sourceError, setSourceError] = useState<Error>()
  const [ticker, setTicker] = useState('')
  const [sources, setSources] = useState<SourceDraft[]>([])
  const model = useQuery({
    queryKey: ['research-status'],
    queryFn: ({ signal }) => api<ModelStatus>('/research/status', { signal }),
    retry: false,
  })
  const inspect = useMutation({
    mutationFn: (body: { ticker: string; sources?: { url: string; published_at: string; text: string }[] }) =>
      api<Inspection>('/research/kalshi/inspect', { method: 'POST', body, session }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['research-evidence'] }) },
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setSourceError(undefined)
    const submitted = sources.map(source => ({ ...source, url: source.url.trim(), text: source.text.trim(), timestamp: Date.parse(source.published) }))
    if (submitted.some(source => !secureUrl(source.url) || !source.text || !Number.isFinite(source.timestamp) || source.timestamp > Date.now())
      || new Set(submitted.map(source => source.url)).size !== submitted.length) {
      setSourceError(new Error('Use fontes públicas distintas, com trechos e datas de publicação válidas.'))
      return
    }
    const published = submitted.map(source => ({
      url: source.url, published_at: new Date(source.timestamp).toISOString(), text: source.text,
    }))
    inspect.mutate({ ticker: ticker.trim().toUpperCase(), ...(published.length ? { sources: published } : {}) })
  }
  const result = inspect.data
  const unavailable = {
    NOT_CONFIGURED: 'A consulta ao DeepSeek depende de uma chave configurada no servidor.',
    NO_RULES: 'A Kalshi não retornou uma regra principal para esta extração.',
    UNAVAILABLE: 'O serviço DeepSeek não retornou citações verificáveis nesta consulta.',
  }
  const book = result?.book
  return <>
    <form className="panel" onSubmit={submit}>
      <div className="panel-heading records-heading">
        <div className="section-name"><span className="panel-icon"><BookOpen size={18} aria-hidden="true" /></span>
          <div><h2>Regras e evidências da Kalshi</h2><p>Consulte o regulamento oficial e confira as citações da análise.</p></div>
        </div>
        {model.data && <Badge tone={model.data.configured ? 'green' : 'amber'}>{model.data.configured ? 'DeepSeek V4.1 Flash disponível' : 'DeepSeek sem chave configurada'}</Badge>}
      </div>
      <div className="form-body">
        {model.isError && <Problem error={model.error} retry={() => void model.refetch()} />}
        <label><span>Código do mercado Kalshi</span><input autoCapitalize="characters" value={ticker} onChange={event => setTicker(event.target.value)}
          required pattern={'[A-Za-z0-9][A-Za-z0-9._\\-]{0,99}'} maxLength={100} placeholder="Ticker oficial" /></label>
        {sources.map((source, index) => <fieldset className="research-fieldset" key={index}>
          <legend>Fonte {index + 1}</legend>
          <label><span>URL pública em HTTPS</span><input type="url" pattern="https://.+" maxLength={600} required value={source.url} onChange={event =>
            setSources(previous => previous.map((item, i) => i === index ? { ...item, url: event.target.value } : item))} /></label>
          <label><span>Publicação no horário local</span><input type="datetime-local" value={source.published} required onChange={event =>
            setSources(previous => previous.map((item, i) => i === index ? { ...item, published: event.target.value } : item))} /></label>
          <label><span>Trecho público da fonte</span><textarea rows={4} maxLength={2000} required value={source.text} onChange={event =>
            setSources(previous => previous.map((item, i) => i === index ? { ...item, text: event.target.value } : item))} /></label>
          <button type="button" onClick={() => setSources(previous => previous.filter((_, i) => i !== index))}>Remover fonte</button>
        </fieldset>)}
        <div className="buttons">
          <button type="button" disabled={sources.length >= 4} onClick={() => setSources(previous => [...previous, { url: '', published: '', text: '' }])}>Adicionar fonte pública</button>
          <button className="button-primary" type="submit" disabled={inspect.isPending}>{inspect.isPending ? 'Consultando…' : 'Consultar evidências'}</button>
        </div>
        {model.data?.configured && <p className="hint">As regras públicas e os trechos adicionados são enviados ao DeepSeek para extração de citações.</p>}
        {sourceError && <Problem error={sourceError} />}
        {inspect.isError && <Problem error={inspect.error} />}
      </div>
    </form>
    {result && <section className="panel">
      <div className="panel-heading records-heading"><div><h2>{result.ticker}</h2><p>Consulta em {dateTime(result.observed_at)}{result.journal_id ? ' · Registro ' + result.journal_id : ''}</p></div>
        <Badge tone="amber">Revisão humana pendente</Badge>
      </div>
      <div className="form-body">
        <dl className="research-rules">
          <div><dt>Regra principal</dt><dd>{result.rules.primary || '—'}</dd></div>
          {result.rules.secondary && <div><dt>Regra complementar</dt><dd>{result.rules.secondary}</dd></div>}
          <div><dt>Fecha em</dt><dd>{dateTime(result.rules.closes_at)}</dd></div>
        </dl>
        {book && <details className="form-details" open>
          <summary>Preço executável e profundidade</summary>
          {book.status === 'OBSERVED' ? <>
            <p className="hint">Livro observado em {dateTime(book.observed_at)}.</p>
            <DataTable title="Melhores ofertas da Kalshi" empty="Sem ofertas observadas" rows={[
              ...(book.yes_ask ? [{ label: 'Sim', ...book.yes_ask }] : []),
              ...(book.no_ask ? [{ label: 'Não', ...book.no_ask }] : []),
            ]} columns={[
              { key: 'label', label: 'Lado', value: row => valueText(row.label) },
              { key: 'price', label: 'Preço (USD)', value: row => number(typeof row.price === 'number' ? row.price : string(row.price), 4), numeric: true },
              { key: 'quantity', label: 'Contratos', value: row => number(typeof row.quantity === 'number' ? row.quantity : string(row.quantity), 2), numeric: true },
            ]} />
          </> : <p className="hint">Livro de ofertas indisponível nesta consulta.</p>}
        </details>}
        <details className="form-details"><summary>Fontes da consulta</summary>
          <ul className="research-citations">{result.sources.map(source => <li key={source.id}><SourceLink source={source} /></li>)}</ul>
        </details>
        {result.extraction ? <>
          <h3>Extração do DeepSeek{result.extraction.ambiguous ? ' · há ambiguidade' : ''}</h3>
          <dl className="research-rules">
            <div><dt>Evento</dt><dd>{result.extraction.event}</dd></div>
            <div><dt>Prazo extraído</dt><dd>{dateTime(result.extraction.deadline)}</dd></div>
            <div><dt>Fonte oficial citada</dt><dd>{result.extraction.resolution_source ?? '—'}</dd></div>
          </dl>
          <ol className="research-citations">{result.extraction.facts.map((fact, index) => {
            const source = result.sources.find(item => item.id === fact.source_id)
            return <li key={index}><strong>{fact.claim}</strong><blockquote>{fact.quote}</blockquote>{source && <SourceLink source={source} />}</li>
          })}</ol>
        </> : <Empty title="Extração por IA indisponível" description={result.model_status && result.model_status !== 'EXTRACTED'
          ? unavailable[result.model_status] : 'As regras acima vieram da consulta oficial.'} />}
      </div>
    </section>}
  </>
}

export function ResearchPage({ session }: { session: Session }) {
  const [section, setSection] = useState<'quant' | 'kalshi'>('quant')
  return <>
    <PageTitle title="Inteligência" description="Valide estratégias e revise as evidências que sustentam os sinais." />
    <div className="segmented" role="group" aria-label="Área de pesquisa">
      <button type="button" aria-pressed={section === 'quant'} onClick={() => setSection('quant')}>Validação quantitativa</button>
      <button type="button" aria-pressed={section === 'kalshi'} onClick={() => setSection('kalshi')}>Pesquisa Kalshi</button>
    </div>
    <div className="page-stack research-view" hidden={section !== 'quant'}><QuantitativeStudy /></div>
    {section === 'kalshi' && <KalshiResearch session={session} />}
  </>
}
