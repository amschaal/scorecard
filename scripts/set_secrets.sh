#!/usr/bin/env bash
# Populate the Secrets Manager secrets created by the base CDK stack. Usage:
#   scripts/set_secrets.sh [env-name] [aws-profile]
# Prompts for DATABASE_URL (unless the base stack created the database and filled it in itself)
# and writes COLLECTOR_TOKENS from freshly generated tokens, one per cluster in $CLUSTERS
# (comma-separated, default "hive").
set -euo pipefail
export AWS_REGION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["context"].get("region", "us-west-2"))' \
                    "$(dirname "$0")/../infra/cdk.json")  # same region as the stacks
ENV_NAME=${1:-prod}
PROFILE=${2:-}
STACK_BASE="HpcUsage-${ENV_NAME}-base"
STACK_APP="HpcUsage-${ENV_NAME}"
AWSP=(aws); [[ -n "$PROFILE" ]] && AWSP+=(--profile "$PROFILE")

out() { "${AWSP[@]}" cloudformation describe-stacks --stack-name "$1" \
        --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null; }

DB_SECRET=$(out "$STACK_BASE" DatabaseUrlSecretArn)
TOK_SECRET=$(out "$STACK_BASE" CollectorTokensSecretArn)
[[ -n "$DB_SECRET" && -n "$TOK_SECRET" ]] || { echo "stack $STACK_BASE not found: run 'make deploy STACKS=$STACK_BASE' first" >&2; exit 1; }
CLUSTERS=${CLUSTERS:-hive}

DB_ENDPOINT=$(out "$STACK_BASE" DatabaseEndpoint)
if [[ -n "$DB_ENDPOINT" ]]; then
  echo "database-url already set by the stack (database=create, $DB_ENDPOINT); leaving it alone"
else
  read -r -s -p "DATABASE_URL (postgresql://user:pass@host:5432/db): " DBURL; echo
  "${AWSP[@]}" secretsmanager put-secret-value --secret-id "$DB_SECRET" --secret-string "$DBURL" >/dev/null
  echo "set $DB_SECRET"
fi

json="{"
for c in ${CLUSTERS//,/ }; do
  t=$(python3 "$(dirname "$0")/gen_token.py")
  json+="\"$c\":\"$t\","
  echo "token for $c: $t   (put in ~/.hpcusage/config.ini on that cluster)"
done
json="${json%,}}"
"${AWSP[@]}" secretsmanager put-secret-value --secret-id "$TOK_SECRET" --secret-string "$json" >/dev/null
echo "set $TOK_SECRET"

SERVICE_ARN=$(out "$STACK_APP" ServiceArn)
if [[ -n "$SERVICE_ARN" ]]; then
  echo "The service already exists; trigger a deployment so it picks up the new values:"
  echo "  aws apprunner start-deployment --service-arn $SERVICE_ARN"
else
  echo "Next: make push && make deploy"
fi
