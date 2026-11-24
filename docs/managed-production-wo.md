# CAU-PROD-001 · Managed hosting and account settings

Baseline `d5521af81b922ca80ffb35910cca1828b9d4abe1` on `codex/cau-mvp-001`. This increment prepares a private, online CAUCHE PULSE instance for the owner. It uses a managed Cloudflare Worker and Container at a workers.dev hostname and a dedicated Supabase PostgreSQL database.

## Account boundaries

Demo credentials remain in the original connection settings. In **Conta → Configurações da conta**, the real account form uses independent encrypted storage and can verify the selected account and present its balance and currency. An unsuccessful read clears the remembered real balance. The worker accepts only the existing REPLAY, PAPER and BROKER_DEMO modes. Real connector classes expose only balance retrieval on fixed HTTPS GET destinations. The real Deriv connection requires an explicit active real Options account ID. Deriv's account listing endpoint specifies the upstream trade scope even though CAUCHE only calls the list endpoint. For Kalshi, prefer an API key scoped to `read::portfolio_balance`.

[Deriv Get All Accounts](https://developers.deriv.com/docs/options/get-accounts/) · [Deriv published venue schemas](https://github.com/deriv-com/deriv-api-schemas/releases) · [Kalshi Get Balance](https://docs.kalshi.com/api-reference/portfolio/get-balance.md)

## Database provisioning

The project uses the [private schema setup](../deploy/supabase/0001_private_schema.sql). A dedicated runtime role has schema creation privileges and begins without LOGIN. Configure its login secret at cutover. Supabase API roles have no schema access. Alembic initializes the application tables during the managed runtime startup.

## Release acceptance

- Build the managed image and verify migrations `0001` and `0002` on PostgreSQL, including upgrade from the previous release.
- Keep the app schema outside the Supabase Data API. Verify the runtime role and TLS identity over a direct or session-mode connection.
- Use owner-only Cloudflare Access before any container route opens. Confirm anonymous and wrong-identity denial, secure app cookie and WSS in the hosted environment.
- Confirm Cloudflare billing authorization, background worker uptime and restart recovery with the actual managed plan.
- Verify database backup and restore in a separate environment. Enter demo and real credentials only in their respective protected settings, then verify status and balance.
- Track any future real-money order behavior in a separate Work Order. Managed hosting and account reads leave real execution disabled.

Runbook [Cloudflare production setup](cloudflare-production.md). This increment does not promote the MVP checkpoint or waive outstanding external acceptance.
