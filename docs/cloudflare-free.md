# CAUCHE PULSE on the free plans

The initial owner-only account console runs at [cauche-pulse.kayzendev.workers.dev](https://cauche-pulse.kayzendev.workers.dev). Cloudflare Workers serves the interface and API. Hyperdrive connects to the dedicated Supabase PostgreSQL project in São Paulo. There are no cron jobs, containers, or paid services in this profile.

The owner can save, verify, and disconnect Deriv and Kalshi accounts from **Configurações da conta**. The demo and real tabs have independent encrypted credentials. Verification reads the selected account balance. The real Deriv account ID must identify an active real Options account. The free profile includes the [research workspace](research.md) for browser-based research and public Kalshi evidence capture. The Python runtime runs broker demo execution and strategy backtests.

## Build and test

```sh
npm --prefix frontend ci
npm --prefix frontend run build:free
npm --prefix cloudflare ci
npm --prefix cloudflare test
cd cloudflare
npx wrangler deploy --config wrangler.free.jsonc --dry-run
```

## Provision and update

1. Apply the [free account schema](../deploy/supabase/0002_free_account_settings.sql) and [research journal migration](../deploy/supabase/0003_free_research_journal.sql) to the dedicated Supabase project. Keep the schema outside the Data API. Enable login on its dedicated runtime role with an independently generated database password.
2. Upload the public Supabase CA to Cloudflare. Create a Hyperdrive configuration using the direct PostgreSQL endpoint, the dedicated role, the imported CA, and `--sslmode verify-full --caching-disabled`. Assign its ID to the `CAUCHE_DB` binding.
3. Set Worker secrets `APP_ORIGIN`, `CAUCHE_OWNER_EMAIL`, `CAUCHE_OWNER_PASSWORD_HASH`, and `CAUCHE_FREE_KEY`. Generate 32 random bytes encoded as base64url for the owner password and store its lowercase SHA-256 hex digest. Generate another 32 random bytes encoded as standard base64 for the encryption key. Preserve an encrypted backup of the encryption key before storing broker credentials.
4. Deploy with `npx wrangler deploy --config wrangler.free.jsonc`. For initial provisioning, pass the secret JSON from a private location with `--secrets-file`. Later deployments preserve the Worker secrets. The free and managed configurations share the same Worker name and should only be deployed during an intentional profile change.
5. Check that anonymous connections receive 401, cross-origin writes and missing CSRF receive 403, the owner can save and remove test credentials in each tab, and the old session cookie is rejected after logout. Use venue-provided credentials for upstream authentication acceptance.

Rotate the owner by updating `CAUCHE_OWNER_PASSWORD_HASH` with a new cryptographically generated password and deleting existing rows in `cauche_free.sessions` through the project administrator. The encryption key has a different lifecycle. Existing credentials become unreadable if the encryption key is replaced.

The free plan limits compute and database queries. A paused Supabase project must be restored before login or changes are available.

[Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/) · [Hyperdrive pricing](https://developers.cloudflare.com/hyperdrive/platform/pricing/) · [Supabase project pausing](https://supabase.com/docs/guides/platform/project-pausing)
