# Deployment

Expected local entry point: docker compose up --build. This will expose the site on a loopback address and keep the database internal. Bootstrap the first administrator with the CLI using a password prompt. Copy the example environment file for optional venue credentials or advanced settings. Never commit that file.

Use authenticated TLS termination for access beyond loopback. Back up with pg_dump and validate restoration in an isolated database. Store the master credential separately. Stop the bot, verify open broker orders, and stop Compose without deleting its volume before upgrading. Check current acceptance in the evidence bundle.
