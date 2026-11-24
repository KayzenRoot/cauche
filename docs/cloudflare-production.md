# Managed Cloudflare deployment

The private owner portal runs at `https://cauche-pulse.<account-subdomain>.workers.dev`. A Cloudflare Worker admits the configured owner through a hostname-based Cloudflare Access application and proxies HTTPS and WebSocket requests to one managed container. The container runs FastAPI, the built SPA and the separate execution worker. Supabase PostgreSQL retains users, encrypted integration settings, event history and execution state. The container filesystem is ephemeral. [Container architecture](https://developers.cloudflare.com/containers/concepts/architecture/) · [workers.dev](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/)

The Cloudflare Workers Paid plan is required for Containers. Instance, Durable Object, Worker, and egress usage are metered. The configuration selects `standard-1`, and headless operation can keep it running each hour of the month. Approve the account and an operating budget for that usage before a hosted deployment. Cloudflare recommends a custom domain for business-critical workloads. Its Durable Object scheduling policy for Containers is currently public beta. [Container pricing](https://developers.cloudflare.com/containers/platform/pricing/) · [Scheduling policies](https://developers.cloudflare.com/containers/configuration/scheduling-policy/)

## Provision a separate database

Use the dedicated managed Supabase project and a dedicated production LOGIN role whose name begins with `cauche_`. Keep the `cauche` schema off the project's Data API exposed-schema list, revoke privileges from public usage roles and set the dedicated role's default `search_path` to `cauche`. Grant the app role ownership or the DDL rights it requires in that schema. Keep the local demo user, history and Fernet key in the local database. The managed startup checks `current_schema()` before running Alembic.

In the Supabase Connect panel, choose Direct connection if IPv6 reachability is verified or choose Session pooler for IPv4 clients. Both use port `5432`. Use the dedicated role in the connection DSN, with the project-ref suffix on the session pooler user. The worker holds session-level PostgreSQL advisory locks. A transaction pooler on `6543` cannot provide those semantics. Download the project root CA from Database Settings and enforce SSL. The runtime uses that CA and `sslmode=verify-full` to verify server identity. [Supabase connection modes](https://supabase.com/docs/guides/database/connecting-to-postgres) · [TLS verification with the Session pooler](https://supabase.com/docs/guides/database/psql)

## Configure the private hostname

1. Determine the exact `workers.dev` account subdomain through the authorized Cloudflare account. The Worker name is `cauche-pulse`.
2. In Zero Trust, create a self-hosted Access application for the exact Worker hostname with all paths covered. Create an Allow policy listing only the owner's verified identity through the chosen identity provider. Use the application AUD from this Access app. Worker-level Access blocks WebSocket upgrades. Hostname-based Access supports the intended WSS admission path and still requires hosted verification. [Cloudflare Access configuration](https://developers.cloudflare.com/workers/configuration/cloudflare-access/)
3. Deploy only an approved revision. Preview URLs are disabled in Wrangler. The application Worker rejects requests that lack the expected exact origin, its Access AUD, or the verified owner email. The managed runtime requires its database, credential key, and owner configuration before it can serve traffic.

## Set runtime bindings

Set the following values using the authorized Worker's secret management. Supply them interactively or through a protected secret manager. Keep the database password, owner password, master key and broker credentials out of Git, Docker build arguments, and command-line values.

| Name | Purpose |
| --- | --- |
| `APP_ORIGIN` | Exact HTTPS Worker origin without a trailing slash |
| `CF_ACCESS_AUD` | AUD of the exact hostname-based Access application |
| `CAUCHE_OWNER_EMAIL` | Normalized email of the actual owner, also used for Access |
| `DATABASE_URL` | Supabase direct or Session pooler DSN on port 5432, using the dedicated LOGIN role |
| `CAUCHE_DB_CA_PEM` | Contents of the root CA from the authorized Supabase project |
| `CAUCHE_MASTER_KEY` | A fresh Fernet key generated for this deployment |
| `CAUCHE_BOOTSTRAP_PASSWORD` | Unique owner password of 14–128 characters for the first start only |
| `CAUCHE_CONTINUOUS` | Set to `1` only after validating the hosted site and approving continuous billing |
| `LOG_RETENTION_DAYS` | Optional retention setting, default 90 days |

The first admitted request starts the container. It runs migrations under a PostgreSQL advisory lock and creates the registered owner if the database is empty and the bootstrap password was supplied. Existing owner email and administrator role must match the configuration. The container removes the bootstrap password from the environment passed to the API and execution processes. After confirming the first login, remove `CAUCHE_BOOTSTRAP_PASSWORD` from Worker secrets and follow the controlled restart below. Keep the database and master key backups independently protected. [Cloudflare runtime secrets](https://developers.cloudflare.com/containers/examples/env-vars-and-secrets/)

Every change to a runtime binding passed to the container requires a new container start, including removal of the bootstrap password. Stop and reconcile execution, disable continuous operation, and close browser tabs or other clients. Wait for the singleton to idle out or verify all active operations have closed and stop it in Cloudflare. Apply the Worker binding changes and send one admitted request after confirming the old instance stopped. Verify cold-start health and owner login before re-enabling continuous operation. A Worker secret update alone leaves a running container's original startup environment in effect, even when the image is unchanged.

## Build and deploy

From the repository root, validate the platform package and build the image. Docker builds the SPA and API from the checked-in lockfiles.

```sh
npm --prefix cloudflare ci
npm --prefix cloudflare test
npm --prefix cloudflare run dry-run
docker build --platform linux/amd64 -f Dockerfile.cloudflare -t cauche-pulse:cloudflare-local .
```

After billing authorization and Access configuration, run `npx wrangler deploy` from `cloudflare/` under the intended Cloudflare account. This uploads the Worker and Docker image to managed Cloudflare services. Expect the endpoint to report unavailable while provisioning completes. Set secrets for the correct Worker under that account. The configured minute Cron will start or refresh the singleton container only if `CAUCHE_CONTINUOUS` equals `1`. Without it, authenticated traffic starts the container on demand. The private system can operate unattended after the schedule is enabled, but interruption and cold recovery must be tested under the chosen plan. [Managed deployment](https://developers.cloudflare.com/containers/guides/deploy/) · [Restart behavior](https://developers.cloudflare.com/containers/faq/)

## Production acceptance

- Verify the Worker responds only on the intended HTTPS hostname. A browser without an Access session must receive a Cloudflare Access challenge or denial when opening the SPA and `/api/health`. An identity outside the allowlist must be denied. A direct preview alias should not resolve to the app.
- Authenticate through Access as the owner and check `/api/health` reports `database=OK` and `worker=ONLINE`. Verify a browser without a CAUCHE session receives HTTP 401 from `/api/auth/session` even after Access sign-in. Sign into CAUCHE and verify a `Secure`, `HttpOnly`, `SameSite=Strict` app session cookie. Confirm the browser connects to `wss://<hostname>/api/ws` and reconnects with its cursor after a container restart.
- Confirm Alembic reports the expected revision in `cauche.alembic_version` and no CAUCHE tables appear in an exposed schema. Verify TLS and that `current_schema()` is `cauche` from the runtime connection.
- Run Replay, then Paper and confirm persisted events across a container replacement. Configure dedicated Deriv and Kalshi demo accounts through the owner's Connections settings. Verify venue account/balance and authentication, then test one controlled demo order and reconciliation only if credentials exist and demo placement is authorized. Configure real-account credentials through the real/read-only settings and verify balance snapshots through official authenticated reads. Real credentials are separate from demo execution.
- Keep headless operation disabled until the release has passed the venue and outage checks. Before any restart test, stop the bot and reconcile pending demo orders. Verify state survives an idle shutdown and wake-up. Inspect provider logs for an online worker and an expected resume from persisted state.
- Export the database through a direct TLS-verified connection using `pg_dump -Fc`, or use the provider backup plan. Rehearse `pg_restore --exit-on-error` in a separate empty project. Validate access to the matching Fernet key outside the database. Review coverage, compute, log retention and backlog growth before enabling continuous usage. [Supabase backups](https://supabase.com/docs/guides/platform/backups)

## Cutover and rollback

Before deploying an updated image, stop the bot, reconcile venue outcomes, and capture a restorable backup. Disable `CAUCHE_CONTINUOUS`, close open application WebSockets and allow the five-minute inactivity timer to expire. If the old container remains running after deployment, the Durable Object compares its recorded image with the new digest. It returns a temporary 503 and skips port requests and keepalive until the old container stops. If an open connection delays the drain, verify closure of all active operations before stopping the singleton in Cloudflare. After the new container starts, verify the database migration, owner access and browser feed before resuming. [Named image updates](https://developers.cloudflare.com/containers/guides/image-management/#roll-out-a-named-image-update)

For rollback, keep the Access gate enabled and keep execution stopped. Roll back the Worker and its image to the previously verified revision only when it is compatible with the existing database schema. Otherwise restore the backup to an isolated replacement project and verify the historical DSN and master key before cutover. Do not rewind a production database past an unreconciled broker event. Removing edge access or turning off Cron preserves Supabase state.
