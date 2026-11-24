CREATE TABLE cauche_free.research_settlements (
    ticker text NOT NULL CHECK (ticker ~ '^[A-Z0-9][A-Z0-9._-]{0,99}$'),
    contract_key text NOT NULL CHECK (contract_key ~ '^[0-9a-f]{64}$'),
    due_at timestamptz NOT NULL,
    terms jsonb NOT NULL CHECK (jsonb_typeof(terms) = 'object'),
    next_check_at timestamptz NOT NULL,
    lease_id uuid,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 96),
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SETTLED', 'RULES_CHANGED', 'UNAVAILABLE')),
    result text CHECK (result IN ('YES', 'NO')),
    settled_at timestamptz,
    observed_at timestamptz,
    source text,
    PRIMARY KEY (ticker, contract_key),
    CHECK (
        (status = 'SETTLED' AND result IS NOT NULL AND settled_at IS NOT NULL
            AND observed_at IS NOT NULL AND source IS NOT NULL AND settled_at <= observed_at)
        OR (status <> 'SETTLED' AND result IS NULL AND settled_at IS NULL AND source IS NULL)
    )
);

CREATE INDEX research_settlements_pending_idx
    ON cauche_free.research_settlements (next_check_at) WHERE status = 'PENDING';
REVOKE ALL ON TABLE cauche_free.research_settlements FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT ON TABLE cauche_free.research_settlements TO cauche_free_runtime;
GRANT UPDATE (next_check_at, lease_id, attempts, status, result, settled_at, observed_at, source)
    ON TABLE cauche_free.research_settlements TO cauche_free_runtime;
