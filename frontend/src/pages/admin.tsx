import { useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, BarChart3, Check, Database, KeyRound, LockKeyhole, Plug, Save, Server,
  ShieldAlert, ShieldCheck, SlidersHorizontal, Terminal, Unplug,
} from 'lucide-react'
import { api, params } from '../api'
import type { StreamStatus } from '../api'
import { Badge, DataTable, Empty, Feed, Loading, PageTitle, Problem, Status } from '../components/common'
import { ConfirmDialog } from '../components/confirm'
import type { Confirmation } from '../components/confirm'
import { ParameterFields } from '../components/parameters'
import { dateTime, exchangeNames, field, localText, money, number, relativeTime, strategyName } from '../format'
import type { Connection, EventsPage, Exchange, List, Overview, RecordData, RiskConfig, Scalar, Scope, Session, Strategy } from '../types'
import { HistoryPage, LogsPage } from './records'

const nav = [
  { to: 'visao-geral', label: 'Visão operacional', Icon: Activity },
  { to: 'conexoes', label: 'Conexões', Icon: Plug },
  { to: 'estrategias', label: 'Estratégias', Icon: SlidersHorizontal },
  { to: 'risco', label: 'Risco', Icon: ShieldAlert },
  { to: 'resultados', label: 'Resultados', Icon: BarChart3 },
  { to: 'sistema', label: 'Sistema', Icon: Server },
  { to: 'auditoria', label: 'Auditoria', Icon: Terminal },
  { to: 'seguranca', label: 'Segurança', Icon: LockKeyhole },
]

const adminKeys = ['admin-overview', 'admin-connections', 'admin-real-connections', 'admin-audit', 'admin-risk', 'admin-sessions', 'strategies', 'dashboard']
function useRefreshAdmin() {
  const client = useQueryClient()
  return () => {
    for (const key of adminKeys) void client.invalidateQueries({ queryKey: [key] })
  }
}

function OverviewPage({ system = false }: {system?: boolean}) {
  const overview = useQuery({
    queryKey: ['admin-overview'],
    queryFn: ({ signal }) => api<Overview>('/admin/overview', { signal }),
    refetchInterval: 10000,
  })
  const data = overview.data
  const status = (entry: unknown) => typeof entry === 'string' ? entry
    : entry && typeof entry === 'object' ? String(field(entry as RecordData, 'status', 'state') ?? 'UNKNOWN') : undefined
  return <>
    <PageTitle title={system ? 'Sistema' : 'Visão operacional'}
      description={system ? 'Acompanhe serviços, estado do worker e versão instalada.' : 'Monitoramento dos serviços do terminal e das conexões de demonstração.'} />
    {overview.isPending ? <Loading /> : overview.isError ? <Problem error={overview.error} retry={() => void overview.refetch()} />
      : data && <>
        <div className="metric-strip">
          {[
            { name: 'API', state: data.status, Icon: Activity, info: 'Versão ' + data.version },
            { name: 'Banco de dados', state: status(data.database), Icon: Database, info: 'Persistência' },
            { name: 'Worker', state: status(data.worker), Icon: Server, info: dateTime(data.bot?.heartbeat_at) },
            { name: 'Motor', state: data.bot?.state, Icon: SlidersHorizontal, info: number(data.uptime_seconds, 0) + ' s de atividade da API' },
          ].map(({ name, state, Icon, info }) => <section className="panel metric" key={name}>
            <div className="metric-heading"><h2>{name}</h2><Icon size={17} aria-hidden="true" /></div>
            <Status state={state} /><small>{info}</small>
          </section>)}
        </div>
        <div className="split-grid">
          <section className="panel">
            <div className="panel-heading"><h2>Conexões em demo</h2><Link className="quiet-link" to="/admin/conexoes">Gerenciar</Link></div>
            {data.connections?.length ? <div className="form-body">
              {data.connections.map(item => <div className="setting-row" key={item.exchange}>
                <div><strong>{exchangeNames[item.exchange]}</strong><small>{relativeTime(item.last_sync)}</small></div>
                <Status state={item.status} />
              </div>)}
            </div> : <Empty title="Sem conexões configuradas" />}
          </section>
          <section className="panel">
            <div className="panel-heading"><h2>{system ? 'Eventos de sistema' : 'Atividade recente'}</h2><Link className="quiet-link" to={system ? '/admin/auditoria' : '/eventos'}>Ver registros</Link></div>
            <Feed events={(data.events ?? []).filter(event => !system || ['SYSTEM', 'ERROR', 'API'].includes(event.type)).slice(0, 8)} compact />
          </section>
        </div>
        {system && data.log_retention_days !== undefined && <section className="panel setting-banner">
          <div><h2>Retenção operacional</h2><p>Configurada no ambiente do servidor.</p></div>
          <Badge>{number(data.log_retention_days, 0)} dias</Badge>
        </section>}
      </>}
  </>
}

type AccountEnvironment = Connection['environment']

const accountEnvironments = {
  BROKER_DEMO: { label: 'Demo oficial', path: '/admin/connections', queryKey: 'admin-connections' },
  REAL_READ_ONLY: { label: 'Conta real · somente leitura', path: '/admin/real-connections', queryKey: 'admin-real-connections' },
} satisfies Record<AccountEnvironment, {label: string; path: string; queryKey: string}>

interface CredentialField {
  id: string
  label: string
  secret?: boolean
  optional?: boolean
  multiline?: boolean
}

function credentialFields(exchange: Exchange, isReal: boolean): CredentialField[] {
  if (exchange === 'DERIV') return [
    { id: 'app_id', label: 'ID do aplicativo' },
    { id: 'token', label: isReal ? 'Token da conta real' : 'Token demo', secret: true },
    { id: 'account_id', label: isReal ? 'ID da conta real' : 'ID da conta demo (opcional)', optional: !isReal },
  ]
  return [
    { id: 'key_id', label: isReal ? 'ID da chave da conta real' : 'ID da chave demo' },
    { id: 'private_key', label: 'Chave privada PEM', secret: true, multiline: true },
  ]
}

function ConnectionForm({ environment, exchange, connection, session }: {
  environment: AccountEnvironment; exchange: Exchange; connection?: Connection; session: Session;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [enabled, setEnabled] = useState<boolean>()
  const [confirm, setConfirm] = useState<Confirmation>()
  const refresh = useRefreshAdmin()
  const environmentConfig = accountEnvironments[environment]
  const path = environmentConfig.path + '/' + exchange
  const isReal = environment === 'REAL_READ_ONLY'
  const accountLabel = isReal ? 'da conta real' : 'da demo'
  const capabilities = Array.isArray(connection?.capabilities) ? undefined : connection?.capabilities
  const currency = capabilities?.currency
  const reportedBalance = isReal ? connection?.balance : capabilities?.demo_balance
  const balance = typeof reportedBalance === 'string' || typeof reportedBalance === 'number' ? reportedBalance : undefined
  const observedCurrency = typeof currency === 'string' && /^[A-Z]{3}$/.test(currency) ? currency : undefined
  const formattedBalance = observedCurrency ? money(balance, observedCurrency) : number(balance, 6)
  const active = enabled ?? connection?.enabled ?? true
  const action = useMutation({
    mutationFn: (kind: 'save' | 'remove' | 'test') => {
      if (kind === 'remove') return api<Connection>(path + params({ confirm: true }), { method: 'DELETE', session })
      return api<Connection>(path + (kind === 'test' ? '/test' : ''), {
        method: kind === 'test' ? 'POST' : 'PUT', session,
        body: kind === 'test' ? { confirm: true }
          : { enabled: active, confirm: true, credentials: Object.fromEntries(Object.entries(draft).filter(([, value]) => value !== '')) },
      })
    },
    onSuccess: (_, kind) => {
      if (kind !== 'test') { setDraft({}); setEnabled(undefined) }
      refresh()
    },
  })
  const ask = (kind: 'save' | 'remove' | 'test') => setConfirm({
    title: kind === 'remove' ? (isReal ? 'Desconectar a conta real?' : 'Desconectar a demo?')
      : kind === 'test' ? `Testar conexão ${accountLabel}?` : `Salvar credenciais ${accountLabel}?`,
    description: kind === 'remove' ? `As credenciais deste ambiente serão removidas para a ${exchangeNames[exchange]}.`
      : kind === 'test' ? `O servidor fará uma consulta autenticada ${isReal ? 'na conta real' : 'na demo'} da ${exchangeNames[exchange]}.`
      : `O servidor armazenará de forma criptografada as credenciais deste ambiente para a ${exchangeNames[exchange]}.`,
    destructive: kind === 'remove',
    confirm: () => action.mutate(kind),
  })
  return <form className="panel" autoComplete="off" onSubmit={event => { event.preventDefault(); ask('save') }}>
    <div className="panel-heading">
      <div className="section-name"><span className="panel-icon"><Plug size={19} aria-hidden="true" /></span>
        <div><h2>{exchangeNames[exchange]}</h2><p>{environmentConfig.label} · {connection?.configured ? 'Credenciais protegidas' : 'Sem credenciais'}</p></div>
      </div>
      <Status state={connection?.status ?? 'NOT_CONFIGURED'} />
    </div>
    <div className="form-body">
      <p className="hint">Última sincronização <time>{dateTime(connection?.last_sync)}</time></p>
      {balance !== undefined && <div className="setting-row">
        <strong>{isReal ? 'Saldo real observado' : 'Saldo demo observado'}</strong>
        <span>{formattedBalance}{observedCurrency && ` ${observedCurrency}`}</span>
      </div>}
      {!isReal && connection?.capabilities && (Array.isArray(connection.capabilities) ? connection.capabilities.length > 0 : Object.keys(connection.capabilities).length > 0) &&
        <details className="form-details"><summary>Recursos disponíveis</summary><pre>{JSON.stringify(connection.capabilities, null, 2)}</pre></details>}
      {credentialFields(exchange, isReal).map(item => <label key={item.id}><span>{item.label}</span>
        {item.multiline ? <textarea rows={4} spellCheck={false} autoComplete="off" className="secret-key" required
          value={draft[item.id] ?? ''} onChange={event => setDraft(previous => ({ ...previous, [item.id]: event.target.value }))} />
          : <input type={item.secret ? 'password' : 'text'} required={!item.optional}
            value={draft[item.id] ?? ''} autoComplete={item.secret ? 'new-password' : 'off'}
            onChange={event => setDraft(previous => ({ ...previous, [item.id]: event.target.value }))} />}
      </label>)}
      <label className="check-field"><input type="checkbox" checked={active} onChange={event => setEnabled(event.target.checked)} /><span>Habilitar conexão</span></label>
      {action.isError && <Problem error={action.error} />}
      {action.isSuccess && <p className="feedback" role="status"><Check size={17} aria-hidden="true" /> {action.variables === 'test'
        ? 'Teste concluído · ' + localText(action.data?.status) : action.variables === 'remove' ? 'Conexão removida' : 'Configuração salva'}</p>}
      <div className="buttons">
        <button type="submit" className="button-primary" disabled={action.isPending}><Save size={15} aria-hidden="true" /> Salvar conexão</button>
        <button type="button" disabled={action.isPending || !connection?.configured} onClick={() => ask('test')}><Activity size={15} aria-hidden="true" /> Testar conexão</button>
        <button type="button" disabled={action.isPending || !connection?.configured} onClick={() => ask('remove')}><Unplug size={15} aria-hidden="true" /> Desconectar</button>
      </div>
    </div>
    {confirm && <ConfirmDialog value={confirm} onClose={() => setConfirm(undefined)} />}
  </form>
}

function ConnectionsPage({ session }: {session: Session}) {
  const [environment, setEnvironment] = useState<AccountEnvironment>('BROKER_DEMO')
  const config = accountEnvironments[environment]
  const connections = useQuery({
    queryKey: [config.queryKey],
    queryFn: ({ signal }) => api<List<Connection>>(config.path, { signal }),
  })
  return <>
    <PageTitle title="Conexões" />
    <section className="panel setting-banner" aria-label="Configuração por ambiente">
      <div>
        <h2>Ambiente da conta</h2>
        <p>{environment === 'REAL_READ_ONLY' ? 'Consulte o saldo da conta real com acesso de leitura.' : 'Gerencie as credenciais usadas para operar em demonstração.'}</p>
      </div>
      <div className="segmented" role="group" aria-label="Selecionar ambiente">
        {(Object.keys(accountEnvironments) as AccountEnvironment[]).map(option =>
          <button key={option} type="button" aria-pressed={environment === option} onClick={() => setEnvironment(option)}>
            {accountEnvironments[option].label}
          </button>)}
      </div>
    </section>
    {connections.isPending ? <Loading /> : connections.isError ? <Problem error={connections.error} retry={() => void connections.refetch()} />
      : <div className="split-grid">
        {(['DERIV', 'KALSHI'] as const).map(exchange => <ConnectionForm key={environment + exchange} environment={environment} exchange={exchange}
          session={session} connection={connections.data?.items.find(item => item.exchange === exchange)} />)}
      </div>}
  </>
}

function StrategyForm({ strategy, session }: {strategy: Strategy; session: Session}) {
  const [draft, setDraft] = useState<Record<string, Scalar>>({})
  const [enabled, setEnabled] = useState<boolean>()
  const [confirm, setConfirm] = useState<Confirmation>()
  const client = useQueryClient()
  const refresh = useRefreshAdmin()
  const active = enabled ?? strategy.enabled
  const save = useMutation({
    mutationFn: () => api<Strategy>('/admin/strategies/' + strategy.id, {
      method: 'PUT', session, body: { enabled: active, parameters: { ...strategy.parameters, ...draft }, confirm: true },
    }),
    onSuccess: result => {
      client.setQueryData<List<Strategy>>(['strategies'], prior => ({ items: (prior?.items ?? []).map(item => item.id === result.id ? result : item) }))
      setDraft({})
      setEnabled(undefined)
      refresh()
    },
  })
  return <form className="panel" onSubmit={event => {
    event.preventDefault()
    setConfirm({
      title: 'Salvar estratégia?',
      description: 'O motor passará a usar os parâmetros aprovados para novas avaliações desta estratégia.',
      confirm: () => save.mutate(),
    })
  }}>
    <div className="panel-heading strategy-heading">
      <div><h2>{strategyName(strategy.id, strategy.name)}</h2><p>{exchangeNames[strategy.exchange]} · {strategy.id} · v{strategy.version}</p></div>
      <Status state={strategy.enabled ? 'ACTIVE' : 'DISABLED'} />
    </div>
    <div className="form-body">
      <label className="check-field"><input type="checkbox" checked={active} onChange={event => setEnabled(event.target.checked)} /><span>Habilitar estratégia</span></label>
      <details className="form-details">
        <summary>Editar parâmetros</summary>
        <ParameterFields values={{ ...strategy.parameters, ...draft }} definitions={strategy.parameter_schema}
          update={(key, value) => setDraft(previous => ({ ...previous, [key]: value }))} />
      </details>
      {save.isError && <Problem error={save.error} />}
      {save.isSuccess && <p className="feedback" role="status"><Check size={16} aria-hidden="true" /> Estratégia salva</p>}
      <button type="submit" disabled={save.isPending || enabled === undefined && Object.keys(draft).length === 0}><Save size={15} aria-hidden="true" /> Salvar estratégia</button>
    </div>
    {confirm && <ConfirmDialog value={confirm} onClose={() => setConfirm(undefined)} />}
  </form>
}

function StrategiesPage({ session, strategies }: {session: Session; strategies: Strategy[]}) {
  return <>
    <PageTitle title="Estratégias" description="Edite parâmetros e habilite estratégias para novas sessões de execução." />
    {strategies.length ? <div className="split-grid">
      {strategies.map(strategy => <StrategyForm key={strategy.id} strategy={strategy} session={session} />)}
    </div> : <Empty title="Estratégias indisponíveis" description="Confira a saúde da API no painel de sistema." />}
  </>
}

function RiskPage({ session }: {session: Session}) {
  const [draft, setDraft] = useState<Record<string, Scalar>>({})
  const [confirm, setConfirm] = useState<Confirmation>()
  const client = useQueryClient()
  const refresh = useRefreshAdmin()
  const data = useQuery({ queryKey: ['admin-risk'], queryFn: ({ signal }) => api<RiskConfig>('/admin/risk', { signal }) })
  const violations = useQuery({
    queryKey: ['events-live', 'risk-blocks'],
    queryFn: ({ signal }) => api<EventsPage>('/events?type=RISK&limit=20', { signal }),
  })
  const save = useMutation({
    mutationFn: (body: RiskConfig) => api<RiskConfig>('/admin/risk', { method: 'PUT', session, body: { ...body, confirm: true } }),
    onSuccess: result => {
      client.setQueryData(['admin-risk'], result)
      setDraft({})
      refresh()
    },
  })
  const risk = data.data
  const toggle = () => {
    if (!risk) return
    const next = !risk.kill_switch
    setConfirm({
      title: next ? 'Ativar bloqueio global?' : 'Liberar bloqueio global?',
      description: next ? 'Novas exposições em qualquer ambiente serão impedidas pelo motor de risco.'
        : 'O motor voltará a avaliar novas exposições conforme os limites configurados.',
      destructive: !next,
      confirm: () => save.mutate({ ...risk, kill_switch: next }),
    })
  }
  return <>
    <PageTitle title="Gerenciamento de risco" description="Defina limites de exposição e acompanhe os bloqueios do motor." />
    {data.isPending ? <Loading /> : data.isError ? <Problem error={data.error} retry={() => void data.refetch()} /> : risk && <>
      <section className="panel setting-banner">
        <div className="section-name"><span className="panel-icon"><ShieldAlert size={20} aria-hidden="true" /></span>
          <div><h2>Bloqueio global</h2><p>{risk.kill_switch ? 'O motor bloqueia novas exposições.' : 'Novas exposições são avaliadas pelos limites.'}</p></div>
        </div>
        <div className="buttons">
          <Badge tone={risk.kill_switch ? 'red' : 'green'} dot>{risk.kill_switch ? 'Bloqueio ativo' : 'Bloqueio inativo'}</Badge>
          <button type="button" disabled={save.isPending} className={risk.kill_switch ? undefined : 'button-danger'} onClick={toggle}>
            {risk.kill_switch ? 'Liberar bloqueio global' : 'Ativar bloqueio global'}
          </button>
        </div>
      </section>
      <form className="panel" onSubmit={event => {
        event.preventDefault()
        setConfirm({
          title: 'Salvar limites de risco?',
          description: 'Os limites se aplicam às novas decisões de exposição e são validados pelo servidor.',
          confirm: () => save.mutate({ ...risk, thresholds: { ...risk.thresholds, ...draft } }),
        })
      }}>
        <div className="panel-heading"><h2>Limites de operação</h2></div>
        <div className="form-body">
          <ParameterFields values={{ ...risk.thresholds, ...draft }} update={(key, value) => setDraft(previous => ({ ...previous, [key]: value }))} />
          {save.isError && <Problem error={save.error} />}
          {save.isSuccess && <p className="feedback" role="status"><Check size={16} /> Configuração atualizada</p>}
          <button className="button-primary" type="submit" disabled={save.isPending || Object.keys(draft).length === 0}><Save size={15} /> Salvar limites</button>
        </div>
      </form>
      <section className="panel"><div className="panel-heading"><h2>Bloqueios recentes</h2></div>
        {violations.isPending ? <Loading /> : violations.isError ? <Problem error={violations.error} retry={() => void violations.refetch()} />
          : <Feed events={violations.data?.items ?? []} />}
      </section>
    </>}
    {confirm && <ConfirmDialog value={confirm} onClose={() => setConfirm(undefined)} />}
  </>
}

function SecurityPage({ session }: {session: Session}) {
  const [current, setCurrent] = useState('')
  const [password, setPassword] = useState('')
  const [repeat, setRepeat] = useState('')
  const [confirm, setConfirm] = useState<Confirmation>()
  const [mismatch, setMismatch] = useState(false)
  const client = useQueryClient()
  const sessions = useQuery({
    queryKey: ['admin-sessions'],
    queryFn: ({ signal }) => api<List<RecordData>>('/admin/sessions', { signal }),
  })
  const revoke = useMutation({
    mutationFn: (id: string) => api('/admin/sessions/' + encodeURIComponent(id) + '?confirm=true', { method: 'DELETE', session }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['admin-sessions'] })
      void client.invalidateQueries({ queryKey: ['admin-audit'] })
    },
  })
  const save = useMutation({
    mutationFn: () => api<void>('/auth/password', {
      method: 'POST', session, body: { current_password: current, new_password: password, confirm: true },
    }),
    onSuccess: () => {
      setCurrent('')
      setPassword('')
      setRepeat('')
      void client.invalidateQueries({ queryKey: ['session'] })
    },
  })
  return <>
    <PageTitle title="Segurança" description="Gerencie a senha e consulte o histórico das sessões." />
    <div className="split-grid">
      <form className="panel" onSubmit={event => {
        event.preventDefault()
        setMismatch(password !== repeat)
        if (password === repeat) setConfirm({
          title: 'Alterar senha?',
          description: 'A troca invalida as sessões anteriores. Entre com a nova senha quando solicitado.',
          confirm: () => save.mutate(),
        })
      }}>
        <div className="panel-heading">
          <div className="section-name"><span className="panel-icon"><KeyRound size={18} aria-hidden="true" /></span>
            <div><h2>Trocar senha</h2><p>{session.email}</p></div>
          </div>
        </div>
        <div className="form-body">
          <label><span>Senha atual</span><input required autoComplete="current-password" type="password" value={current} onChange={event => setCurrent(event.target.value)} /></label>
          <label><span>Nova senha</span><input required minLength={14} maxLength={128} autoComplete="new-password" type="password" value={password} onChange={event => setPassword(event.target.value)} /></label>
          <label><span>Confirme a nova senha</span><input required minLength={14} maxLength={128} autoComplete="new-password" type="password" value={repeat} onChange={event => setRepeat(event.target.value)} /></label>
          {mismatch && <Problem error={new Error('As senhas novas não coincidem.')} />}
          {save.isError && <Problem error={save.error} />}
          {save.isSuccess && <p className="feedback" role="status"><Check size={16} /> Senha atualizada</p>}
          <button className="button-primary" type="submit" disabled={save.isPending}><LockKeyhole size={15} aria-hidden="true" /> Atualizar senha</button>
        </div>
      </form>
      <section className="panel">
        <div className="panel-heading">
          <div className="section-name"><span className="panel-icon"><ShieldCheck size={18} aria-hidden="true" /></span>
            <div><h2>Sessões</h2><p>Registro de acesso ao terminal</p></div>
          </div>
          <Link className="quiet-link" to="/admin/auditoria">Auditoria</Link>
        </div>
        {sessions.isPending ? <Loading /> : sessions.isError ? <Problem error={sessions.error} retry={() => void sessions.refetch()} />
          : <DataTable title="Sessões do usuário" empty="Sem sessões registradas" rows={sessions.data?.items ?? []} columns={[
            { key: 'date', label: 'Criada em', value: row => dateTime(field(row, 'created_at', 'timestamp') as string) },
            { key: 'expiry', label: 'Expira em', value: row => dateTime(field(row, 'expires_at') as string) },
            { key: 'status', label: 'Estado', value: row => row.current ? <Badge tone="lime">Sessão atual</Badge>
              : <Badge tone="green">Ativa</Badge> },
            { key: 'action', label: 'Ação', value: row => !row.current && typeof row.id === 'string' ? <button
              type="button" disabled={revoke.isPending} onClick={() => setConfirm({
                title: 'Revogar sessão?', description: 'A sessão selecionada perderá o acesso ao terminal.',
                destructive: true, confirm: () => revoke.mutate(String(row.id)),
              })}>Revogar sessão</button> : '—' },
          ]} />}
        {revoke.isError && <Problem error={revoke.error} />}
      </section>
    </div>
    {confirm && <ConfirmDialog value={confirm} onClose={() => setConfirm(undefined)} />}
  </>
}

export function AdminPages({ session, scope, update, strategies, stream }: {
  session: Session; scope: Scope; update: (value: Scope) => void; strategies: Strategy[]; stream: StreamStatus;
}) {
  if (session.role.toLowerCase() !== 'admin') return <div className="panel">
    <Empty icon="security" title="Acesso administrativo restrito" description="Entre com uma conta com permissão de administração." />
  </div>
  return <>
    <nav className="admin-nav" aria-label="Seções administrativas">
      {nav.map(({ to, label, Icon }) => <NavLink key={to} to={'/admin/' + to}><Icon size={16} aria-hidden="true" /> {label}</NavLink>)}
    </nav>
    <Routes>
      <Route index element={<Navigate to="visao-geral" replace />} />
      <Route path="visao-geral" element={<OverviewPage />} />
      <Route path="conexoes" element={<ConnectionsPage session={session} />} />
      <Route path="estrategias" element={<StrategiesPage session={session} strategies={strategies} />} />
      <Route path="risco" element={<RiskPage session={session} />} />
      <Route path="resultados" element={<HistoryPage scope={scope} update={update} strategies={strategies} analytics />} />
      <Route path="sistema" element={<OverviewPage system />} />
      <Route path="auditoria" element={<LogsPage strategies={strategies} stream={stream} audit />} />
      <Route path="seguranca" element={<SecurityPage session={session} />} />
      <Route path="*" element={<Navigate to="visao-geral" replace />} />
    </Routes>
  </>
}
