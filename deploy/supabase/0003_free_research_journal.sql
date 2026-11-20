CREATE TABLE cauche_free.research_observations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker text NOT NULL CHECK (ticker ~ '^[A-Z0-9][A-Z0-9._-]{0,99}$'),
    observed_at timestamptz NOT NULL,
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object')
);
REVOKE ALL ON TABLE cauche_free.research_observations FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT ON TABLE cauche_free.research_observations TO cauche_free_runtime;
