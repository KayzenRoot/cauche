# Architecture

React/TypeScript/Vite frontend and charting at a same-origin reverse proxy. FastAPI owns authentication and validated commands. PostgreSQL with SQLAlchemy and Alembic stores durable state. A separate Python worker serializes strategy ticks and recovery. Structured event records support cursor-based WebSocket catch-up. Normalized exchange observations carry exchange, source and UTC timestamps.

Use exact decimal values for amounts. Acquire a PostgreSQL transaction lock for simulation and intent persistence. Persist a broker submission claim before network writes. An ambiguous broker write must block further submissions until reconciled. Browser code receives sanitized state only.
