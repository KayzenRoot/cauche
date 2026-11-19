# Database model

Users and sessions establish ownership. Exchange connections store encrypted configuration and operational state. Strategy configurations and runs version the strategy decision context. Market snapshots and candles retain provenance. Probability estimates and signals retain observations and decision reasons. Order intents retain the risk decision with idempotency constraints. Orders, fills, positions, settlements, fees and balance snapshots retain financial history. System events, audit events and risk violations are independently queryable. Backtest runs retain input/version/configuration and deterministic results.

Use UTC, fixed-point numeric columns for monetary values, unique broker and internal IDs, and indexes for mode/exchange/time and incremental event queries.
