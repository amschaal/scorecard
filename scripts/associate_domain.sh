#!/usr/bin/env bash
# One-time: associate the campus hostname with the App Runner service, then print the DNS records
# to request (App Runner issues the ACM certificate once the validation CNAMEs exist). Idempotent.
# Run inside `make cdk-shell` with your own credentials (AWS_CONFIG_DIR=~/.aws):
#   scripts/associate_domain.sh [env-name] [domain]     # domain defaults to `domain` in infra/cdk.json
set -euo pipefail
ENV_NAME=${1:-prod}
DOMAIN=${2:-$(jq -er '.context.domain' "$(dirname "$0")/../infra/cdk.json")}
ARN=$(aws cloudformation describe-stacks --stack-name "HpcUsage-${ENV_NAME}" \
      --query "Stacks[0].Outputs[?OutputKey=='ServiceArn'].OutputValue" --output text 2>/dev/null || true)
[[ -n "$ARN" && "$ARN" != None ]] || { echo "stack HpcUsage-${ENV_NAME} not found: run 'make deploy-app' first" >&2; exit 1; }

if [[ -n $(aws apprunner describe-custom-domains --service-arn "$ARN" \
             --query "CustomDomains[?DomainName=='$DOMAIN'].DomainName" --output text) ]]; then
  echo "$DOMAIN is already associated with $ARN"
else
  aws apprunner associate-custom-domain --service-arn "$ARN" --domain-name "$DOMAIN" --no-enable-www-subdomain >/dev/null
  echo "associated $DOMAIN with $ARN"
fi
exec "$(dirname "$0")/domain_records.sh" "$ENV_NAME"
