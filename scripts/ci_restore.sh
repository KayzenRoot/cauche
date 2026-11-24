#!/usr/bin/env bash
# Only the disposable Compose stack of the GitHub-hosted integration job.
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted ]]; then
  echo "Restore rehearsal is restricted to GitHub-hosted CI." >&2
  exit 2
fi
umask 077
dump="$(mktemp)"
trap 'rm -f -- "$dump"' EXIT
# Freeze all writers; never restore into the source database.
docker compose stop web api worker
docker compose exec -T db pg_dump -U cauche -d cauche -Fc --no-owner --no-privileges > "$dump"
test -s "$dump"
docker compose exec -T db createdb -U cauche cauche_restore_ci
docker compose exec -T db pg_restore --exit-on-error --no-owner --no-privileges -U cauche -d cauche_restore_ci < "$dump"
docker compose run --rm --no-deps -T -e CAUCHE_TEST_PASSWORD api python - < scripts/ci_restore_check.py
