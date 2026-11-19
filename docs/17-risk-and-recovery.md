# Risk and recovery

Gate all exposure centrally, using limits per order, strategy, venue, market, event and portfolio. Block by kill switch, loss, drawdown, concurrency, order rate, cooldown, stale data, low liquidity, spread/payout, balance discrepancies and recurring failures. Keep limits conservative and cap administrative increases. Continue accounting and reconciliation when opening exposure is disabled.

An unknown write outcome is an outstanding exposure. Record it before retry decisions. Resume only after a trustworthy lookup or explicit operator reconciliation. Simulated fills occur within transactional local state and are idempotent. Retain worker and event cursors and restore on restart.
