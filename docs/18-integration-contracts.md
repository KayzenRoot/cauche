# Integration contracts

Connectors accept explicit venue-native inputs, Decimal amounts and selected demo credentials. They return normalized market data or sanitized broker-confirmed state. They cannot accept a caller-selected write host. Never retry an ambiguous mutation automatically. Market data observations include venue, environment, freshness and source. The risk decision and order intent precede any adapter submission. Deployment and test environments use synthetic fixtures with deterministic times and no external credentials.
