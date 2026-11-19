# API contracts

All application paths start with /api. JSON uses English field names and decimal strings where applicable. Browser mutations send Origin and X-CSRF-Token, except login which requires Origin.

- GET /api/health: service/database/worker health. POST /api/auth/login {email,password}, GET /api/auth/session, POST /api/auth/logout, POST /api/auth/password {current_password,new_password}. Session responses carry email, role and csrf_token.
- GET /api/dashboard and GET /api/market: filters mode, exchange, strategy, instrument. Dashboard includes metrics, orders, equity, events and bot. Market includes source, status, instrument and candles in UTC epoch seconds and OHLC.
- GET /api/bot and POST /api/bot/{action}: start/pause/resume/stop. Start has {mode,exchange,strategy,instrument}. Validate transitions and acknowledge worker state.
- GET /api/events: after, limit, exchange, mode, strategy, severity, q, start and end filters. Return {items,next_cursor}. GET /api/history: order/fill/position/settlement state. GET /api/events/export and /api/history/export: scoped CSV.
- WebSocket /api/ws?after=0: Origin and authenticated session required. Deliver JSON events in cursor order.
- GET /api/strategies, PUT /api/admin/strategies/{strategy}: {enabled,parameters}. GET/PUT /api/admin/risk: thresholds and kill_switch. GET /api/admin/overview, /api/admin/audit, /api/admin/connections. PUT/DELETE /api/admin/connections/{exchange}, POST /api/admin/connections/{exchange}/test. Connection responses contain masked state.
- GET/POST /api/backtests: run or retrieve reproducible simulation. POST takes exchange, strategy, instrument, start/end, initial_balance, parameters and costs as applicable.

Implementation may evolve field details with the frontend and record changes in the evidence bundle.
