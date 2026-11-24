#!/usr/bin/env bash
# Only the disposable Compose stack of the GitHub-hosted integration job.
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted ]]; then
  echo "Restore rehearsal is restricted to GitHub-hosted CI." >&2
  exit 2
fi
umask 077
compose() {
  if [[ -n "${CAUCHE_COMPOSE_ENV_FILE:-}" ]]; then
    docker compose --env-file "$CAUCHE_COMPOSE_ENV_FILE" "$@"
  else
    docker compose "$@"
  fi
}
dump="$(mktemp)"
trap 'rm -f -- "$dump"' EXIT
# Freeze all writers; never restore into the source database.
compose stop web api worker
compose exec -T db pg_dump -U cauche -d cauche -Fc --no-owner --no-privileges > "$dump"
test -s "$dump"
compose exec -T db createdb -U cauche cauche_restore_ci
compose exec -T db pg_restore --exit-on-error --no-owner --no-privileges -U cauche -d cauche_restore_ci < "$dump"
compose run --rm --no-deps -T \
  -v "${CAUCHE_TEST_PASSWORD_FILE:?Set CAUCHE_TEST_PASSWORD_FILE}:/tmp/owner-password:ro" \
  -e CAUCHE_TEST_PASSWORD_FILE=/tmp/owner-password \
  api python - < scripts/ci_restore_check.py
