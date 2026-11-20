import { lazy, StrictMode, Suspense, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Link, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MotionConfig, motion } from 'motion/react'
import {
  Activity, ArrowRight, CandlestickChart, ChartNoAxesCombined, Clock3, Eye, EyeOff, FlaskConical,
  History, LayoutDashboard, LockKeyhole, LogOut, PanelLeftClose, PanelLeftOpen, Radio, Settings2,
  ShieldCheck, Terminal, UserRound,
} from 'lucide-react'
import '@fontsource-variable/manrope'
import '@fontsource-variable/space-grotesk'
import './styles.css'
import { ApiError, api, useEventStream } from './api'
import { Badge, Brand, Loading, Problem } from './components/common'
import type { List, Scope, Session, Strategy } from './types'

const TradingPage = lazy(() => import('./pages/trading').then(module => ({ default: module.TradingPage })))
const BacktestsPage = lazy(() => import('./pages/backtests').then(module => ({ default: module.BacktestsPage })))
const HistoryPage = lazy(() => import('./pages/records').then(module => ({ default: module.HistoryPage })))
const LogsPage = lazy(() => import('./pages/records').then(module => ({ default: module.LogsPage })))
const AdminPages = lazy(() => import('./pages/admin').then(module => ({ default: module.AdminPages })))
const ConnectionsPage = lazy(() => import('./pages/admin').then(module => ({ default: module.ConnectionsPage })))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      retry: (count, error) => (!(error instanceof ApiError) || error.status >= 500 || error.status === 0) && count < 1,
      refetchOnReconnect: true,
    },
    mutations: { retry: false, gcTime: 0 },
  },
})

function Login() {
  const client = useQueryClient()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [visible, setVisible] = useState(false)
  const login = useMutation({
    mutationFn: () => api<Session>('/auth/login', { method: 'POST', body: { email, password } }),
    onSuccess: session => {
      setPassword('')
      client.removeQueries({ predicate: query => query.queryKey[0] !== 'session' })
      client.setQueryData(['session'], session)
    },
  })
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    login.mutate()
  }
  return <main className="login-layout">
    <section className="login-art" aria-label="CAUCHE PULSE">
      <Brand />
      <div className="login-raster" aria-hidden="true">
        <svg viewBox="0 0 760 430" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
          <defs>
            <linearGradient id="pulse-gradient"><stop stopColor="#82d329" stopOpacity="0.03" /><stop offset="0.55" stopColor="#b5ff39" /><stop offset="1" stopColor="#b5ff39" stopOpacity="0.08" /></linearGradient>
            <radialGradient id="pulse-glow"><stop stopColor="#b5ff39" stopOpacity=".22" /><stop offset="1" stopColor="#b5ff39" stopOpacity="0" /></radialGradient>
          </defs>
          <circle cx="380" cy="214" r="206" fill="url(#pulse-glow)" />
          <g fill="none" stroke="#45722a" strokeOpacity=".45">
            <circle cx="380" cy="214" r="74" /><circle cx="380" cy="214" r="122" /><circle cx="380" cy="214" r="170" />
            <path d="M380 17v394M185 214h390" strokeDasharray="3 8"/>
            <path d="M220 76h-28v27m376-27h28v27m-376 252h-28v-27m376 27h28v-27" />
          </g>
          <path d="M0 236h148l44-22h42l23 28 25-68 33 118 45-169 30 146 32-77 25 22h56l49-27h208" stroke="url(#pulse-gradient)" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" fill="none" />
          <circle cx="360" cy="123" r="6" fill="#b5ff39" fillOpacity=".17" /><circle cx="360" cy="123" r="2.6" fill="#b5ff39" />
        </svg>
      </div>
      <div className="login-tagline"><span><Activity size={16} aria-hidden="true" /> Automated Trading Intelligence</span>
        <h2>Estratégia,<br />com controle.</h2>
        <p>{import.meta.env.MODE === 'cauche-free' ? 'Configure suas contas e confira a conexão com as corretoras.' : 'Simule, acompanhe e audite suas decisões em um terminal privado.'}</p>
      </div>
    </section>
    <div className="login-access">
      <div className="login-mobile-brand"><Brand /></div>
      <motion.form className="login-form" onSubmit={submit} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .2 }}>
        <span className="login-lock"><LockKeyhole size={22} aria-hidden="true" /></span>
        <h1>Acesse o terminal</h1>
        <p>Entre com sua conta de proprietário.</p>
        <label><span>E-mail</span><input aria-label="E-mail" type="email" autoComplete="username" required value={email} onChange={event => setEmail(event.target.value)} placeholder="nome@exemplo.com" /></label>
        <label><span>Senha</span><div className="password-field"><input aria-label="Senha" type={visible ? 'text' : 'password'} autoComplete="current-password" required
          value={password} onChange={event => setPassword(event.target.value)} />
          <button className="icon-button" type="button" aria-label={visible ? 'Ocultar senha' : 'Mostrar senha'} onClick={() => setVisible(value => !value)}>
            {visible ? <EyeOff size={17} aria-hidden="true" /> : <Eye size={17} aria-hidden="true" />}
          </button></div>
        </label>
        {login.isError && <Problem error={login.error} />}
        <button className="button-primary login-submit" type="submit" disabled={login.isPending}>{login.isPending ? 'Entrando…' : 'Entrar'} <ArrowRight size={17} aria-hidden="true" /></button>
      </motion.form>
      <div className="login-footer"><ShieldCheck size={16} aria-hidden="true" /><span>Acesso restrito ao proprietário</span></div>
    </div>
  </main>
}

const navigation = [
  { to: '/', name: 'Visão geral', Icon: LayoutDashboard },
  { to: '/terminal', name: 'Terminal', Icon: CandlestickChart },
  { to: '/backtests', name: 'Backtests', Icon: FlaskConical },
  { to: '/historico', name: 'Histórico', Icon: History },
  { to: '/eventos', name: 'Eventos', Icon: Terminal },
  { to: '/admin', name: 'Administração', Icon: Settings2 },
]

function Clock() {
  const [time, setTime] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setTime(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  return <time className="wall-clock" dateTime={time.toISOString()}><Clock3 size={14} aria-hidden="true" />
    {new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC', hourCycle: 'h23' }).format(time)} UTC</time>
}

function Workspace({ session }: {session: Session}) {
  const [scope, setScope] = useState<Scope>({ exchange: 'DERIV', mode: 'REPLAY', strategy: '', instrument: 'EUR/USD', start: '', end: '' })
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('cauche-sidebar-collapsed') === 'true' } catch { return false }
  })
  const [accountOpen, setAccountOpen] = useState(false)
  const client = useQueryClient()
  const location = useLocation()
  const stream = useEventStream(session)
  const strategies = useQuery({
    queryKey: ['strategies'],
    queryFn: ({ signal }) => api<List<Strategy>>('/strategies', { signal }),
  })
  const logout = useMutation({
    mutationFn: () => api<void>('/auth/logout', { method: 'POST', session }),
    onSuccess: () => {
      client.setQueryData(['session'], null)
      client.removeQueries({ predicate: query => query.queryKey[0] !== 'session' })
    },
  })
  const admin = session.role.toLowerCase() === 'admin'
  const strategyList = strategies.data?.items ?? []
  const toggle = () => {
    try { localStorage.setItem('cauche-sidebar-collapsed', String(!collapsed)) } catch { /* storage can be disabled */ }
    setCollapsed(value => !value)
  }
  const link = ({ to, name, Icon }: (typeof navigation)[number]) => <NavLink
    to={to} end={to === '/'} key={to} title={name}><Icon size={19} aria-hidden="true" /><span>{name}</span>
  </NavLink>
  return <div className={'workspace' + (collapsed ? ' sidebar-collapsed' : '')}>
    <a href="#main-content" className="skip-link">Pular para o conteúdo</a>
    <aside className="sidebar" aria-label="Menu principal">
      <div className="sidebar-brand"><NavLink to="/" title="CAUCHE PULSE"><Brand compact={collapsed} /></NavLink></div>
      <nav aria-label="Navegação principal">
        <span className="nav-heading">Operação</span>
        {navigation.slice(0, 4).map(link)}
        <span className="nav-heading">Monitoramento</span>
        {navigation.slice(4).filter(item => item.to !== '/admin' || admin).map(link)}
      </nav>
      <div className="sidebar-foot">
        <div className="sidebar-policy"><LockKeyhole size={16} aria-hidden="true" /><span>Execução real bloqueada</span></div>
        <button type="button" className="sidebar-toggle" onClick={toggle} aria-expanded={!collapsed} title={collapsed ? 'Expandir menu' : 'Recolher menu'}>
          {collapsed ? <PanelLeftOpen size={18} aria-hidden="true" /> : <PanelLeftClose size={18} aria-hidden="true" />}
          <span>{collapsed ? 'Expandir menu' : 'Recolher menu'}</span>
        </button>
      </div>
    </aside>
    <div className="workspace-main">
      <header className="topbar">
        <div className="topbar-left"><ChartNoAxesCombined size={17} aria-hidden="true" /><span>Terminal privado</span></div>
        <div className="topbar-right">
          <Badge tone={stream === 'online' ? 'green' : stream === 'connecting' ? 'neutral' : 'amber'} dot>
            {stream === 'online' ? 'Feed conectado' : stream === 'connecting' ? 'Conectando feed' : 'Feed desconectado'}
          </Badge>
          <Clock />
          <div className="account-card">
            <button className="icon-button account-button" aria-label="Conta" type="button" aria-expanded={accountOpen} onClick={() => setAccountOpen(value => !value)}>
              <UserRound size={18} aria-hidden="true" />
            </button>
            {accountOpen && <div className="account-popover">
              <strong>{session.email}</strong>
              <span>{admin ? 'Administrador' : 'Proprietário'}</span>
              {admin && <Link className="button" to="/admin/conexoes" onClick={() => setAccountOpen(false)}>
                <Settings2 size={15} aria-hidden="true" /> Configurações da conta
              </Link>}
              <button type="button" onClick={() => logout.mutate()} disabled={logout.isPending}><LogOut size={15} aria-hidden="true" /> Sair</button>
              {logout.isError && <Problem error={logout.error} />}
            </div>}
          </div>
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="content">
        {strategies.isError && <Problem error={strategies.error} retry={() => void strategies.refetch()} />}
        <motion.div key={location.pathname} className="page-stack" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: .14 }}>
          <Suspense fallback={<Loading />}><Routes>
            <Route path="/" element={<TradingPage scope={scope} update={setScope} strategies={strategyList} session={session} stream={stream} terminal={false} />} />
            <Route path="/terminal" element={<TradingPage scope={scope} update={setScope} strategies={strategyList} session={session} stream={stream} terminal />} />
            <Route path="/backtests" element={<BacktestsPage session={session} strategies={strategyList} />} />
            <Route path="/historico" element={<HistoryPage scope={scope} update={setScope} strategies={strategyList} />} />
            <Route path="/eventos" element={<LogsPage strategies={strategyList} stream={stream} />} />
            <Route path="/admin/*" element={<AdminPages session={session} scope={scope} update={setScope} strategies={strategyList} stream={stream} />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes></Suspense>
        </motion.div>
      </main>
      <footer className="terminal-footer"><span>CAUCHE PULSE <b>v0.1.0</b></span><span><Radio size={14} aria-hidden="true" /> {stream === 'online' ? 'Canal de eventos conectado' : 'Sincronização sujeita à reconexão'}</span></footer>
    </div>
  </div>
}

function FreeWorkspace({ session }: {session: Session}) {
  const client = useQueryClient()
  const logout = useMutation({
    mutationFn: () => api<void>('/auth/logout', { method: 'POST', session }),
    onSuccess: () => {
      client.setQueryData(['session'], null)
      client.removeQueries({ predicate: query => query.queryKey[0] !== 'session' })
    },
  })
  return <div className="workspace sidebar-collapsed">
    <a href="#main-content" className="skip-link">Pular para o conteúdo</a>
    <aside className="sidebar" aria-label="Menu principal">
      <div className="sidebar-brand"><Brand compact /></div>
      <nav aria-label="Navegação principal">
        <NavLink to="/admin/conexoes" title="Configurações da conta"><Settings2 size={19} aria-hidden="true" /><span>Configurações da conta</span></NavLink>
      </nav>
    </aside>
    <div className="workspace-main">
      <header className="topbar">
        <div className="topbar-left"><LockKeyhole size={17} aria-hidden="true" /><span>Terminal privado</span></div>
        <div className="topbar-right">
          <button type="button" onClick={() => logout.mutate()} disabled={logout.isPending}><LogOut size={15} aria-hidden="true" /> Sair</button>
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="content">
        <div className="page-stack">
          {logout.isError && <Problem error={logout.error} />}
          <section className="panel setting-banner">
            <div><h2>Configurações da conta</h2><p>Esta versão permite testar conexões e consultar saldos. O motor de negociação está indisponível.</p></div>
          </section>
          <Suspense fallback={<Loading />}><Routes>
            <Route path="/admin/conexoes" element={<ConnectionsPage session={session} />} />
            <Route path="*" element={<Navigate to="/admin/conexoes" replace />} />
          </Routes></Suspense>
        </div>
      </main>
    </div>
  </div>
}

function App() {
  const client = useQueryClient()
  const session = useQuery({
    queryKey: ['session'],
    queryFn: async ({ signal }) => {
      try { return await api<Session>('/auth/session', { signal }) }
      catch (error) { if (error instanceof ApiError && error.status === 401) return null; throw error }
    },
    staleTime: 30000,
    refetchInterval: 60000,
  })
  useEffect(() => {
    const invalidate = () => { void client.invalidateQueries({ queryKey: ['session'] }) }
    window.addEventListener('cauche-session-expired', invalidate)
    return () => window.removeEventListener('cauche-session-expired', invalidate)
  }, [client])
  if (session.isPending) return <main className="app-pending"><Brand /><Loading /></main>
  if (session.isError) return <main className="app-pending"><Brand /><Problem error={session.error} retry={() => void session.refetch()} /></main>
  if (!session.data) return <Login />
  return import.meta.env.MODE === 'cauche-free'
    ? <FreeWorkspace key={session.data.email} session={session.data} />
    : <Workspace key={session.data.email} session={session.data} />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <MotionConfig reducedMotion="user">
          <App />
        </MotionConfig>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
