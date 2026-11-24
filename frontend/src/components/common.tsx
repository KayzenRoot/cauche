import { Activity, CircleAlert, Database, LoaderCircle, ShieldCheck, Terminal } from 'lucide-react'
import type { ReactNode } from 'react'
import type { EventRecord, RecordData } from '../types'
import { dateTime, eventMessage, localText, statusTone, strategyName } from '../format'
import type { Tone } from '../format'

export function Brand({ compact = false }: {compact?: boolean}) {
  return <span className="brand">
    <img className="brand-mark" src="/favicon.svg" alt="" width="39" height="39" />
    <span className={compact ? 'sr-only' : 'brand-name'}>CAUCHE <b>PULSE</b></span>
  </span>
}

export function PageTitle({ title, description, actions }: {title: string; description?: string; actions?: ReactNode}) {
  return <header className="page-title">
    <div>
      <h1>{title}</h1>
      {description && <p>{description}</p>}
    </div>
    {actions && <div className="page-actions">{actions}</div>}
  </header>
}

export function Badge({ children, tone = 'neutral', dot = false }: {children: ReactNode; tone?: Tone; dot?: boolean}) {
  return <span className={'badge tone-' + tone}>{dot && <span className="status-dot" aria-hidden="true" />}{children}</span>
}

export function Status({ state }: {state?: string | null}) {
  return <Badge tone={statusTone(state)} dot>{localText(state)}</Badge>
}

export function Loading({ compact = false }: {compact?: boolean}) {
  return <div className={compact ? 'loading loading-compact' : 'loading'} role="status">
    <LoaderCircle size={19} aria-hidden="true" /> Sincronizando…
  </div>
}

export function Empty({ title, description, icon = 'data' }: {title: string; description?: string; icon?: 'data' | 'market' | 'events' | 'security'}) {
  const Icon = { data: Database, market: Activity, events: Terminal, security: ShieldCheck }[icon]
  return <div className="empty">
    <span className="empty-icon"><Icon size={22} aria-hidden="true" /></span>
    <strong>{title}</strong>
    {description && <p>{description}</p>}
  </div>
}

export function Problem({ error, retry }: {error: unknown; retry?: () => void}) {
  return <div className="problem" role="alert">
    <CircleAlert size={17} aria-hidden="true" />
    <span>{error instanceof Error ? error.message : 'Não foi possível carregar os dados.'}</span>
    {retry && <button className="text-button" onClick={retry} type="button">Tentar novamente</button>}
  </div>
}

export function Feed({ events, compact = false }: {events: EventRecord[]; compact?: boolean}) {
  if (!events.length) return <Empty icon="events" title="Nenhum evento registrado" description="Os eventos aparecem conforme o motor e os serviços atualizam o estado." />
  return <ol className={compact ? 'event-list event-list-compact' : 'event-list'}>
    {events.map(event => {
      const context = event.context
      return <li key={event.id} className={'event-row event-' + event.type.toLowerCase()}>
        <span className="event-pin" aria-hidden="true" />
        <div className="event-main">
          <div className="event-header">
            <Badge tone={statusTone(event.severity)}>{localText(event.type)}</Badge>
            <time dateTime={event.timestamp}>{dateTime(event.timestamp)}</time>
          </div>
          <p>{eventMessage(event.code, event.message)}</p>
          <div className="event-meta">
            {event.code && <code>{event.code}</code>}
            {event.exchange && <span>{localText(event.exchange)}</span>}
            {event.mode && <span>{localText(event.mode)}</span>}
            {event.instrument && <span>{event.instrument}</span>}
            {event.strategy && <span>{strategyName(event.strategy)}</span>}
          </div>
          {!compact && (event.correlation_id || event.order_id || (context && Object.keys(context).length > 0)) &&
            <details className="event-details">
              <summary>Contexto do evento</summary>
              <dl className="details-list">
                {event.correlation_id && <div><dt>Correlação</dt><dd>{event.correlation_id}</dd></div>}
                {event.order_id && <div><dt>Ordem</dt><dd>{event.order_id}</dd></div>}
              </dl>
              {context && Object.keys(context).length > 0 && <pre>{JSON.stringify(context, null, 2)}</pre>}
            </details>
          }
        </div>
      </li>
    })}
  </ol>
}

export type Column = {key: string; label: string; value: (row: RecordData) => ReactNode; numeric?: boolean}

export function DataTable({ rows, columns, empty, title }: {rows: RecordData[]; columns: Column[]; empty: string; title: string}) {
  if (!rows.length) return <Empty title={empty} />
  return <div className="table-scroll" tabIndex={0} aria-label={title} role="region">
    <table>
      <caption className="sr-only">{title}</caption>
      <thead><tr>{columns.map(column => <th key={column.key} scope="col" className={column.numeric ? 'numeric' : undefined}>{column.label}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={String(row.id ?? row.strategy ?? row.exchange ?? index) + ':' + index}>
        {columns.map(column => <td key={column.key} className={column.numeric ? 'numeric' : undefined}>{column.value(row)}</td>)}
      </tr>)}</tbody>
    </table>
  </div>
}
