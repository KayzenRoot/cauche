import type { ParameterDefinition, Scalar } from '../types'

const parameterNames: Record<string, string> = {
  max_order_exposure: 'Exposição por ordem', max_strategy_exposure: 'Exposição por estratégia',
  max_exchange_exposure: 'Exposição por corretora', max_global_exposure: 'Exposição global',
  max_market_exposure: 'Exposição por mercado', max_event_exposure: 'Exposição por evento',
  max_daily_loss: 'Perda diária máxima', max_drawdown: 'Drawdown máximo',
  max_open_orders: 'Ordens abertas', max_orders_per_minute: 'Ordens por minuto', cooldown_seconds: 'Intervalo mínimo (s)',
  max_spread: 'Spread máximo', min_payout_ratio: 'Payout mínimo', max_age_seconds: 'Idade máxima dos dados (s)',
  min_depth: 'Profundidade mínima', max_errors: 'Limite de erros',
  max_strategy_positions: 'Posições por estratégia', balance_tolerance: 'Tolerância de saldo',
  stake: 'Exposição por operação', fast_period: 'Período da EMA rápida', slow_period: 'Período da EMA lenta',
  rsi_period: 'Período do RSI', adx_period: 'Período do ADX', atr_period: 'Período do ATR',
  ema_fast: 'EMA rápida', ema_slow: 'EMA lenta', donchian_period: 'Período de Donchian',
  min_edge: 'Vantagem mínima', max_inventory: 'Limite de inventário', min_spread: 'Spread mínimo',
  slippage: 'Slippage', fee_rate: 'Taxa proporcional', payout_ratio: 'Multiplicador de pagamento',
  max_volatility: 'Volatilidade máxima', min_volatility: 'Volatilidade mínima', period: 'Período',
  max_notional: 'Valor nocional máximo', max_deviation: 'Desvio máximo',
}

export function ParameterFields({
  values, definitions = {}, update,
}: {values: Record<string, Scalar>; definitions?: Record<string, ParameterDefinition>; update: (key: string, value: Scalar) => void}) {
  const keys = [...new Set([...Object.keys(values), ...Object.keys(definitions)])]
  if (!keys.length) return <p className="hint">Nenhum parâmetro editável neste item.</p>
  return <div className="parameter-grid">
    {keys.map(key => {
      const definition = definitions[key]
      const value = values[key] ?? definition?.default
      const name = parameterNames[key] ?? (key.charAt(0).toUpperCase() + key.slice(1).replaceAll('_', ' '))
      const isBoolean = definition?.type === 'boolean' || typeof value === 'boolean'
      const isNumeric = ['integer', 'number', 'decimal', 'int', 'float'].includes(definition?.type ?? '') || typeof value === 'number'
        || typeof value === 'string' && /^-?\d+(\.\d+)?$/.test(value)
      const isInteger = ['integer', 'int'].includes(definition?.type ?? '') || ['max_open_orders', 'max_orders_per_minute', 'max_age_seconds', 'cooldown_seconds', 'max_errors'].includes(key)
      return <label key={key} className={isBoolean ? 'check-field' : undefined}>
        {isBoolean ? <>
          <input type="checkbox" checked={value === true} onChange={event => update(key, event.target.checked)} />
          <span>{name}</span>
        </> : <>
          <span title={key}>{name}</span>
          <input value={typeof value === 'boolean' ? '' : value ?? ''} type={isNumeric ? 'number' : 'text'}
            step={isNumeric ? (isInteger ? '1' : 'any') : undefined}
            min={definition?.minimum ?? definition?.min} max={definition?.maximum ?? definition?.max}
            inputMode={isNumeric ? 'decimal' : undefined} required
            onChange={event => {
              const next = event.target.value
              update(key, typeof value === 'number' && next !== '' ? Number(next) : next)
            }} />
        </>}
      </label>
    })}
  </div>
}
