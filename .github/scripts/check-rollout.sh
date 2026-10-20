#!/usr/bin/env bash
# After a deploy: every container app in the resource group must have its newest
# revision ready, healthy and running the expected image, and any app that is
# open to the internet must answer /health. In Single revision mode a revision
# that never becomes ready does not take traffic, so the old one keeps serving;
# this script fails the run so that is noticed.
# Usage: check-rollout.sh <resource-group> <expected-image>
# Optional: HEALTH_URL (extra base URL to probe, e.g. a reachable web address).
set -euo pipefail

rg="$1"
image="$2"
timeout_seconds="${ROLLOUT_TIMEOUT_SECONDS:-600}"
deadline=$((SECONDS + timeout_seconds))

apps="$(az containerapp list --resource-group "$rg" --query '[].name' --output tsv)"
if [ -z "$apps" ]; then
  echo "::error::No container apps found in $rg" >&2
  exit 1
fi

for app in $apps; do
  echo "Waiting for $app"
  while true; do
    latest="$(az containerapp show --name "$app" --resource-group "$rg" --query properties.latestRevisionName --output tsv)"
    ready="$(az containerapp show --name "$app" --resource-group "$rg" --query properties.latestReadyRevisionName --output tsv)"
    if [ -n "$latest" ] && [ "$latest" = "$ready" ]; then
      revision="$(az containerapp revision show --name "$app" --resource-group "$rg" --revision "$latest" --output json)"
      health="$(jq -r '.properties.healthState' <<<"$revision")"
      running="$(jq -r '.properties.runningState' <<<"$revision")"
      has_image="$(jq -r --arg image "$image" '[.properties.template.containers[].image] | index($image) != null' <<<"$revision")"
      echo "  $latest health=$health running=$running"
      if [ "$has_image" != "true" ]; then
        echo "::error::$app revision $latest does not run $image" >&2
        exit 1
      fi
      # An app without ingress (the worker) has no probes, so it has no health
      # state to wait for; running is all that can be checked.
      has_ingress="$(az containerapp show --name "$app" --resource-group "$rg" --query 'properties.configuration.ingress != null' --output tsv)"
      if [[ "$running" == Running* ]] && { [ "$health" = "Healthy" ] || [ "$has_ingress" != "true" ]; }; then
        break
      fi
    else
      echo "  latest=$latest ready=$ready"
    fi
    if ((SECONDS >= deadline)); then
      echo "::error::$app did not become healthy within ${timeout_seconds}s" >&2
      exit 1
    fi
    sleep 10
  done
done

probe() {
  local url="$1"
  for attempt in 1 2 3 4 5 6; do
    if curl --fail --silent --show-error --max-time 10 "$url" >/dev/null; then
      echo "  $url ok"
      return 0
    fi
    echo "  $url not ok (attempt $attempt)"
    sleep 10
  done
  echo "::error::$url did not answer" >&2
  return 1
}

# The web app is IP restricted, so the runner cannot reach it; only an app with
# external ingress and no restrictions is probed from here.
for app in $apps; do
  ingress="$(az containerapp show --name "$app" --resource-group "$rg" --query properties.configuration.ingress --output json)"
  if [ "$(jq -r '. != null and .external == true and ((.ipSecurityRestrictions // []) | length == 0)' <<<"$ingress")" = "true" ]; then
    echo "Probing /health on $app"
    probe "https://$(jq -r .fqdn <<<"$ingress")/health"
  fi
done

if [ -n "${HEALTH_URL:-}" ]; then
  echo "Probing /health on HEALTH_URL"
  probe "${HEALTH_URL%/}/health"
fi
