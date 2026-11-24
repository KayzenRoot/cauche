# Deployment

## Loopback installation

Run `docker compose -p cauche-pulse up --build -d --wait` from the repository. The explicit project flag isolates this stack even when the shell defines another Compose project. Visit http://localhost:8080. PostgreSQL, API and worker run on the private Compose network. The reverse proxy binds the host port only to 127.0.0.1.

Create the owner from a terminal:

```sh
docker compose -p cauche-pulse exec api python -m app.bootstrap --email owner@example.test
```

Run the command from the checkout. The bootstrap requires a password of 14–128 characters and prompts for confirmation. A passphrase of several unrelated words can meet the length requirement. The terminal hides typed characters. For an isolated CI environment, pipe a generated password with `--password-file /dev/stdin`. Keep that password out of shell history and source files. The login session uses a loopback-only HTTP development cookie. For private remote access, configure external TLS termination with WebSocket forwarding and set `APP_ORIGIN` to its exact HTTPS origin before recreating the stack. Keep the loopback binding, and use a private network, authenticated tunnel or VPN.

## Optional official demo credentials

Copy the example environment file to a private local environment file. Generate a Fernet key in the running installation using `docker compose exec api python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Put the result in `CAUCHE_MASTER_KEY` and recreate API and worker. Back up the key separately from the database.

Sign in and open Administration → Connections. Save the Deriv PAT, registered App ID and optional demo account ID, or the Kalshi **demo** API key ID and PEM. Use the connection test to confirm authentication and a USD account. The UI only returns masked state. Confirm the connected instrument, allowed contract, account currency and strategy eligibility before starting. Use a dedicated demo account with no unmanaged orders or exposure. Inspect broker records independently before counting demo acceptance. The code uses the official sandbox hosts for mutations.

## Operations

- Read `docker compose ps -a` and `docker compose logs --tail 100 api worker web`. Query `/api/health` and verify schema with `docker compose exec api alembic current --check-heads`.
- Back up by writing `docker compose exec -T db pg_dump -U cauche -d cauche -Fc` to a binary file using a binary-safe shell. Encrypt and protect the backup. Rehearse `pg_restore --exit-on-error` into an isolated empty database before relying on it. Restore the matching original master key separately. A database backup contains encrypted credentials, hashed sessions and trading history.
- For an update, save a backup, stop strategy execution, verify pending broker orders and reconcile ambiguous submissions. Run `docker compose down`, then rebuild and start. Preserve the volume. Automated startup performs Alembic upgrades.
- For safe shutdown, halt execution and confirm open positions or managed expirations in the broker sandbox. Run `docker compose stop`. Keep PostgreSQL state for restart. A Deriv contract can settle while services are offline, so review its subsequent reconciliation.
- If a Deriv acknowledgement is lost, verify the demo account manually. The worker retains the unknown claim and refuses duplicate dispatch. Reconciliation of an ambiguous unidentifiable Deriv purchase requires operator review.

## CI PostgreSQL authentication

The backend CI job starts a disposable PostgreSQL container bound to `127.0.0.1` on a random port. Each run generates a random password, masks it in logs, writes it to a protected temporary file, and configures `scram-sha-256` host authentication. The job verifies that the correct password succeeds and that missing or wrong passwords fail, then removes the container, its volume and the temporary secret files. The Compose integration job uses a separate random database password and a protected temporary environment file; secrets are not persisted in `GITHUB_ENV`.

## Key custody and backup limitations

- The DPAPI master-key escrow is bound to the current Windows user profile. It does not provide cross-device recovery without an explicit export and recovery procedure. Keep an additional passphrase-protected copy off-device if recovery from another device is required.
- The automated backup timer writes encrypted dumps on the host. It does not replicate them off-host by itself; refresh an off-host copy with a separate process. The backup key is stored separately from the ciphertext, and losing it makes the encrypted backups unrecoverable.
- The CA private key is protected by the local user ACL and is not stored on the VPS. Back it up separately and never commit it.
