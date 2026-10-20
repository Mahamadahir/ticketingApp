#!/usr/bin/env bash
# Start the migrate job and wait for it to finish. Fails if it does not succeed.
# Usage: run-migrate-job.sh <job-name> <resource-group>
set -euo pipefail

job="$1"
rg="$2"
timeout_seconds="${MIGRATE_TIMEOUT_SECONDS:-900}"

execution="$(az containerapp job start --name "$job" --resource-group "$rg" --query name --output tsv)"
echo "Started execution $execution of $job"

deadline=$((SECONDS + timeout_seconds))
while true; do
  status="$(az containerapp job execution show --name "$job" --resource-group "$rg" \
    --job-execution-name "$execution" --query properties.status --output tsv)"
  echo "  status: $status"
  case "$status" in
    Succeeded)
      exit 0
      ;;
    Failed | Stopped | Degraded)
      echo "::error::Migrate job ended as $status" >&2
      az containerapp job logs show --name "$job" --resource-group "$rg" \
        --container migrate --execution "$execution" --format text || true
      exit 1
      ;;
  esac
  if ((SECONDS >= deadline)); then
    echo "::error::Migrate job still $status after ${timeout_seconds}s" >&2
    exit 1
  fi
  sleep 10
done
