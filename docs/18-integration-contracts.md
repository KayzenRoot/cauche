# Integration contracts

Connectors use official fixed venue hosts, explicit venue-native inputs, Decimal amounts and selected demo credentials. They expose normalized observations and sanitized broker-confirmed state. They do not follow redirects or accept caller-configured order destinations.

Venue authorization and risk are independent. Every broker intent is persisted and claimed before dispatch. The worker uses PostgreSQL advisory leadership and a locked bot row, and rechecks the configuration before sending. A crash or ambiguous write leaves the claim pending reconciliation. The Kalshi adapter can look up the client order ID. An ambiguous Deriv buy must be reconciled using authorized demo records before opening further exposure.

Replay and fixture Paper inputs are labeled. Observed Paper orders and fills are local simulation. Active Deriv and signed Kalshi demo sessions subscribe to official ticker streams for fresh display prices. Execution decisions use closed candles and bounded request-response book snapshots. The API delivers its own persisted event stream with an independent cursor.

Deriv buyable proposals require a streaming subscription ID. Kalshi order creation and cancellation use the V2 event endpoints. The Kalshi advisory scanner restricts live logical candidates to compatible same-event contracts described by the official MECNET exclusivity metadata. [Deriv proposal comparison](https://developers.deriv.com/comparison/proposal/) · [Kalshi Events API](https://docs.kalshi.com/api-reference/events/get-event.md)
