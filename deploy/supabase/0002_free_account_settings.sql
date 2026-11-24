CREATE ROLE cauche_free_runtime NOLOGIN NOINHERIT;
CREATE SCHEMA cauche_free;
REVOKE ALL ON SCHEMA cauche_free FROM PUBLIC, anon, authenticated, service_role;
GRANT CONNECT ON DATABASE postgres TO cauche_free_runtime;
GRANT USAGE ON SCHEMA cauche_free TO cauche_free_runtime;

CREATE TABLE cauche_free.sessions (
    token_hash text PRIMARY KEY,
    csrf_token text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX ON cauche_free.sessions (expires_at);
CREATE TABLE cauche_free.connections (
    environment text NOT NULL CHECK (environment IN ('BROKER_DEMO', 'REAL_READ_ONLY')),
    exchange text NOT NULL CHECK (exchange IN ('DERIV', 'KALSHI')),
    enabled boolean NOT NULL DEFAULT false,
    ciphertext text,
    status text NOT NULL DEFAULT 'NOT_CONFIGURED',
    currency text,
    balance text,
    verified_at timestamptz,
    revision bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (environment, exchange)
);
INSERT INTO cauche_free.connections (environment, exchange)
SELECT environment, exchange
FROM (VALUES ('BROKER_DEMO'), ('REAL_READ_ONLY')) AS environments(environment)
CROSS JOIN (VALUES ('DERIV'), ('KALSHI')) AS exchanges(exchange);

REVOKE ALL ON ALL TABLES IN SCHEMA cauche_free FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT, DELETE ON cauche_free.sessions TO cauche_free_runtime;
GRANT SELECT, UPDATE ON cauche_free.connections TO cauche_free_runtime;
