import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { EventRecord, Scope, Session } from './types'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

const errors: Record<string, string> = {
  NOT_AUTHENTICATED: 'Sua sessão expirou. Entre novamente.',
  UNAUTHENTICATED: 'Sua sessão expirou. Entre novamente.',
  INVALID_CREDENTIALS: 'E-mail ou senha incorretos.',
  AUTH_ERROR: 'A corretora rejeitou a autenticação.',
  ACCOUNT_NOT_VERIFIED: 'Não foi possível verificar a conta. Confira as credenciais e o ambiente.',
  BROKER_CREDENTIAL_FORMAT: 'Confira o formato dos identificadores e da credencial da corretora.',
  CONNECTION_CHANGED: 'A conexão mudou durante a operação. Confira a configuração atual e tente novamente.',
  CONNECTION_DISABLED: 'Habilite a conexão e salve antes de testar.',
  DERIV_APP_ID_REJECTED: 'A Deriv rejeitou o ID do aplicativo. Use um App ID PAT registrado no portal atual da Deriv.',
  DERIV_TOKEN_REQUIRED: 'Informe o token PAT da Deriv para cadastrar esta conexão.',
  DERIV_TOKEN_REJECTED: 'A Deriv indicou que o token perdeu a validade. Gere um token PAT e salve a nova credencial.',
  DERIV_AUTH_REJECTED: 'A Deriv rejeitou a autenticação. Confira o App ID PAT e se o token está ativo.',
  DERIV_ACCESS_DENIED: 'A Deriv negou acesso à lista de contas. Confira as permissões do token para Options.',
  DERIV_ACCOUNT_NOT_FOUND: 'A Deriv não retornou uma conta com este ID. Confira o identificador da conta Options.',
  DERIV_ENV_ACCOUNT_UNAVAILABLE: 'A Deriv não retornou uma conta Options para este ambiente.',
  DERIV_ACCOUNT_ENV_MISMATCH: 'O ID informado pertence ao outro ambiente da Deriv. Selecione a aba correspondente à conta.',
  DERIV_ACCOUNT_INACTIVE: 'A conta Options selecionada está inativa na Deriv.',
  DERIV_ACCOUNT_SELECTION_REQUIRED: 'A Deriv retornou várias contas. Informe o ID da conta Options que deseja usar.',
  KALSHI_PRIVATE_KEY_INVALID: 'A chave privada da Kalshi precisa ser um PEM válido de Ed25519 ou RSA de pelo menos 2048 bits.',
  KALSHI_KEY_ID_REJECTED: 'A Kalshi não reconheceu o ID da chave neste ambiente. Confira se a chave foi criada na mesma aba, demo ou real.',
  KALSHI_KEY_SIGNATURE_REJECTED: 'A Kalshi rejeitou a assinatura. Confira se o PEM pertence ao ID de chave informado.',
  KALSHI_ACCESS_DENIED: 'A Kalshi negou acesso à conta ou à chave. Confira as restrições de acesso da chave.',
  KALSHI_SCOPE_REQUIRED: 'A Kalshi negou a leitura do saldo. Confira se a chave permite read::portfolio_balance.',
  KALSHI_AUTH_REJECTED: 'A Kalshi rejeitou a autenticação. Confira a chave e o ambiente em que ela foi criada.',
  KALSHI_CLOCK_SKEW: 'A Kalshi rejeitou o horário da assinatura. Tente novamente em instantes.',
  VENUE_RESPONSE_INVALID: 'A corretora retornou uma resposta incompatível. Tente novamente.',
  VENUE_UNAVAILABLE: 'A corretora está temporariamente indisponível. Tente novamente em instantes.',
  NOT_CONFIGURED: 'Configure as credenciais demo na administração.',
  STRATEGY_DISABLED: 'Ative a estratégia na administração antes de iniciar.',
  KILL_SWITCH: 'O bloqueio global está ativado.',
  KILL_SWITCH_ACTIVE: 'O bloqueio global está ativado.',
  UNRESOLVED_EXPOSURE: 'Há exposição pendente. Acompanhe a reconciliação antes de iniciar outra sessão.',
  BROKER_AUTHENTICATION_REQUIRED: 'Teste a conexão demo antes de iniciar.',
  BROKER_NOT_CONFIGURED: 'Configure a conexão demo antes de iniciar.',
  MASTER_KEY_NOT_CONFIGURED: 'Configure a chave-mestra do servidor antes de gravar credenciais.',
  MARKET_DISCOVERY_UNAVAILABLE: 'A corretora não retornou a lista de mercados.',
  INVALID_EVIDENCE: 'Confira o código do mercado, os trechos e as datas das fontes.',
  KALSHI_RULES_UNAVAILABLE: 'A Kalshi não retornou as regras. Tente novamente.',
  KALSHI_MARKET_NOT_FOUND: 'O código informado não corresponde a um mercado da Kalshi.',
  PASSWORD_UNCHANGED: 'Informe uma senha diferente da atual.',
  INVALID_TRANSITION: 'O estado do motor mudou. Confira o status antes de tentar novamente.',
  INVALID_BOT_TRANSITION: 'O estado do motor mudou. Confira o status antes de tentar novamente.',
  CSRF: 'A sessão precisa ser atualizada antes de confirmar esta ação.',
  ORIGIN: 'Abra o terminal pelo endereço configurado no servidor.',
}

export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown; session?: Session; signal?: AbortSignal } = {},
): Promise<T> {
  const { method = 'GET', body, session, signal } = options
  const headers = new Headers({ Accept: 'application/json' })
  if (body !== undefined) headers.set('Content-Type', 'application/json')
  if (session && method !== 'GET') headers.set('X-CSRF-Token', session.csrf_token)
  let response: Response
  try {
    response = await fetch('/api' + path, {
      method, headers, credentials: 'same-origin', cache: 'no-store', signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new ApiError(0, 'A conexão com o servidor está indisponível.')
  }
  if (!response.ok) {
    const error = await response.json().catch(() => null) as {detail?: string | {code?: string}} | null
    const detail = error?.detail
    const code = typeof detail === 'string' ? detail.toUpperCase().replaceAll(' ', '_') : detail?.code
    if (response.status === 401 && !path.startsWith('/auth/')) {
      window.dispatchEvent(new Event('cauche-session-expired'))
    }
    const fallback = {
      400: 'Confira os dados enviados e tente novamente.',
      401: path === '/auth/login' ? 'E-mail ou senha incorretos.' : 'Sua sessão expirou. Entre novamente.',
      403: 'Você não tem permissão para executar esta ação.',
      404: 'Este registro não está disponível.',
      409: 'A operação entrou em conflito com o estado atual.',
      422: 'Um ou mais campos têm valores inválidos.',
      429: 'Muitas tentativas. Aguarde antes de tentar novamente.',
      503: 'Este serviço está indisponível no momento.',
    } as Record<number, string>
    throw new ApiError(response.status, (code && errors[code]) || fallback[response.status] || 'O servidor não conseguiu concluir a operação.')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export function params(values: object): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  }
  return query.size ? '?' + query.toString() : ''
}

export function scopeParams(scope: Scope) {
  return {
    mode: scope.mode,
    exchange: scope.exchange,
    strategy: scope.strategy,
    instrument: scope.instrument,
    start: scope.start,
    end: scope.end,
  }
}

export type StreamStatus = 'connecting' | 'online' | 'offline'

export function useEventStream(session: Session) {
  const client = useQueryClient()
  const [status, setStatus] = useState<StreamStatus>('connecting')
  useEffect(() => {
    let socket: WebSocket | undefined
    let reconnect: number | undefined
    let refresh: number | undefined
    let cursor = 0
    let failures = 0
    let stopped = false
    const keys = new Set<string>()
    const flush = () => {
      refresh = undefined
      for (const key of keys) void client.invalidateQueries({ queryKey: [key] })
      keys.clear()
    }
    const schedule = (...changed: string[]) => {
      changed.forEach(key => keys.add(key))
      if (refresh === undefined) refresh = window.setTimeout(flush, 350)
    }
    const connect = () => {
      if (stopped) return
      const url = new URL('/api/ws' + params({ after: cursor }), window.location.href)
      url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(url)
      socket.addEventListener('open', () => {
        if (stopped) return
        setStatus('online')
        failures = 0
        schedule('dashboard', 'history', 'market', 'bot', 'events-live', 'admin-overview')
      })
      socket.addEventListener('message', ({ data }) => {
        if (stopped || typeof data !== 'string') return
        let event: Partial<EventRecord>
        try {
          event = JSON.parse(data) as Partial<EventRecord>
        } catch {
          return
        }
        if (typeof event?.id !== 'number' || event.id <= cursor) return
        cursor = event.id
        schedule('events-live', 'dashboard')
        if (['MARKET', 'SIGNAL', 'ORDER', 'FILL', 'SETTLEMENT'].includes(event.type ?? '')) schedule('market')
        if (['ORDER', 'FILL', 'SETTLEMENT'].includes(event.type ?? '')) schedule('history')
        if (['SYSTEM', 'RISK', 'STRATEGY', 'ERROR', 'API'].includes(event.type ?? '')) schedule('bot', 'admin-overview')
        if (['ADMIN', 'AUTH'].includes(event.type ?? '')) schedule('admin-audit', 'admin-overview')
      })
      socket.addEventListener('close', ({ code }) => {
        if (stopped) return
        setStatus('offline')
        if (code === 4401 || code === 1008) {
          void client.invalidateQueries({ queryKey: ['session'] })
          return
        }
        reconnect = window.setTimeout(connect, Math.min(15000, 1000 * 2 ** failures++))
      })
    }
    connect()
    return () => {
      stopped = true
      window.clearTimeout(reconnect)
      window.clearTimeout(refresh)
      socket?.close()
    }
  }, [client, session.email])
  return status
}
