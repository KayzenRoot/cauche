# Requirements

- Authenticate a bootstrapped owner, apply RBAC and CSRF, record administrative audit actions, and support session revocation and password changes.
- Drive terminal, histories, charts, backtests and operations views from API state. Stream persisted events and resume by cursor.
- Run six configurable, deterministic strategy implementations with closed input observations and a mandatory independent risk decision.
- Track order lifecycles, partial executions, fees, positions and settlement. Separate per exchange, execution mode and currency.
- Integrate Deriv current Options API and Kalshi Prediction Markets Trade API V2 using demo-only mutations and official market reads.
- Reproduce local startup with Compose and PostgreSQL. Record reproducible test evidence and unresolved acceptance.
