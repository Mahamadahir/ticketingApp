#!/usr/bin/env bash
# Upload and publish the mailbox-change runbooks (infra/runbooks/*.ps1) to the Automation account in the group.
# main.bicep creates the account and the runbook shells; ARM can only take runbook content from a URL, and the repository
# is private, so the script text goes up here with the Azure management API, using the workflow's own identity
# (Contributor on the group is enough). Does nothing when the group has no Automation account (the switch is off).
# Usage: publish-runbooks.sh <resource-group>
set -euo pipefail

rg="$1"
api="2024-10-23"
root="$(cd "$(dirname "$0")/../.." && pwd)"

account="$(az resource list --resource-group "$rg" --resource-type Microsoft.Automation/automationAccounts \
  --query '[0].name' --output tsv)"
if [ -z "$account" ]; then
  echo "No Automation account in $rg (MAILBOX_AUTOMATION_ENABLED is off): no runbooks to publish."
  exit 0
fi

subscription="$(az account show --query id --output tsv)"
base="https://management.azure.com/subscriptions/${subscription}/resourceGroups/${rg}/providers/Microsoft.Automation/automationAccounts/${account}/runbooks"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
python3 "$root/tools/build_runbooks.py" "$work"

for file in "$work"/*.ps1; do
  name="$(basename "$file" .ps1)"
  echo "Uploading $name to $account"
  az rest --method put --url "${base}/${name}/draft/content?api-version=${api}" \
    --headers "Content-Type=text/powershell" --body "@${file}" --output none
  az rest --method post --url "${base}/${name}/publish?api-version=${api}" --output none
done

# Publishing finishes in the background: wait until every runbook says Published.
deadline=$((SECONDS + 300))
for file in "$work"/*.ps1; do
  name="$(basename "$file" .ps1)"
  while true; do
    state="$(az rest --method get --url "${base}/${name}?api-version=${api}" --query properties.state --output tsv)"
    if [ "$state" = "Published" ]; then
      echo "  $name: Published"
      break
    fi
    if ((SECONDS >= deadline)); then
      echo "::error::Runbook $name is still '$state' after 300s" >&2
      exit 1
    fi
    sleep 5
  done
done
