#!/usr/bin/env bash
# Print the DNS records campus DNS must create for the App Runner custom domain.
# Usage: scripts/domain_records.sh [env-name] [aws-profile]
set -euo pipefail
ENV_NAME=${1:-prod}; PROFILE=${2:-}
AWSP=(aws); [[ -n "$PROFILE" ]] && AWSP+=(--profile "$PROFILE")
ARN=$("${AWSP[@]}" cloudformation describe-stacks --stack-name "HpcUsage-${ENV_NAME}" \
      --query "Stacks[0].Outputs[?OutputKey=='ServiceArn'].OutputValue" --output text)
"${AWSP[@]}" apprunner describe-custom-domains --service-arn "$ARN" --output json | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("DNS target (CNAME the app hostname to this):", d["DNSTarget"])
for cd in d.get("CustomDomains", []):
    print("\n", cd["DomainName"], "status:", cd["Status"])
    for r in cd.get("CertificateValidationRecords", []):
        print("  %-6s %s -> %s  [%s]" % (r["Type"], r["Name"], r["Value"], r["Status"]))
'
