#!/usr/bin/env bash
# Refresh AWS creds via ada using values from .env — nothing hardcoded here.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$root/.env" ]; then
  set -a; . "$root/.env"; set +a
fi

: "${ADA_ACCOUNT:?set ADA_ACCOUNT in .env}"
: "${ADA_ROLE:?set ADA_ROLE in .env}"
: "${ADA_PROVIDER:=conduit}"
: "${AWS_PROFILE:=default}"

ada credentials update --once \
  --account="$ADA_ACCOUNT" \
  --provider="$ADA_PROVIDER" \
  --role="$ADA_ROLE" \
  --profile="$AWS_PROFILE"

echo "refreshed creds for profile=$AWS_PROFILE account=$ADA_ACCOUNT"
