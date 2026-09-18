#!/usr/bin/env bash
# Bootstrap CDK for THIS PROJECT ONLY. Instead of the account-wide default (stack CDKToolkit,
# qualifier hnb659fds, AdministratorAccess execution role) this creates a separate bootstrap stack
# whose CloudFormation execution role carries the customer-managed policy built from
# infra/iam/cfn-exec-policy.json -- so a deployment can only touch hpcusage-* resources and
# security groups in the RDS VPC. Qualifier, toolkit stack name and vpc_id come from infra/cdk.json.
#
# Run with ADMIN credentials, once per account/region (and again after editing the policy):
#   AWS_CONFIG_DIR=~/.aws make bootstrap
set -euo pipefail
source "$(dirname "$0")/cdk_env.sh"
POLICY_FILE="$(dirname "$0")/../infra/iam/cfn-exec-policy.json"

[[ "$VPC_ID" != *CHANGE_ME* ]] || { echo "set vpc_id in infra/cdk.json first: the execution policy is pinned to that VPC" >&2; exit 1; }
echo "account $ACCOUNT_ID, region $REGION, qualifier $QUALIFIER, toolkit stack $TOOLKIT_STACK, vpc $VPC_ID"
doc=$(render_policy "$POLICY_FILE")

if aws iam get-policy --policy-arn "$EXEC_POLICY_ARN" >/dev/null 2>&1; then
  # A managed policy keeps at most 5 versions: drop the oldest non-default one when full.
  if [[ $(aws iam list-policy-versions --policy-arn "$EXEC_POLICY_ARN" --query 'length(Versions)' --output text) -ge 5 ]]; then
    oldest=$(aws iam list-policy-versions --policy-arn "$EXEC_POLICY_ARN" \
             --query 'sort_by(Versions[?!IsDefaultVersion], &CreateDate)[0].VersionId' --output text)
    aws iam delete-policy-version --policy-arn "$EXEC_POLICY_ARN" --version-id "$oldest"
  fi
  aws iam create-policy-version --policy-arn "$EXEC_POLICY_ARN" --policy-document "$doc" --set-as-default >/dev/null
  echo "updated policy $EXEC_POLICY_ARN"
else
  aws iam create-policy --policy-name "$EXEC_POLICY_NAME" --policy-document "$doc" \
      --description "CloudFormation execution policy for the $QUALIFIER CDK bootstrap (infra/iam/cfn-exec-policy.json)" \
      --tags Key=project,Value=hpcusage >/dev/null
  echo "created policy $EXEC_POLICY_ARN"
fi

# The explicit aws://account/region target means cdk does not have to synthesize the app (which
# refuses to run until cdk.json is fully filled in) just to discover the environment.
cdk bootstrap "aws://$ACCOUNT_ID/$REGION" \
    --qualifier "$QUALIFIER" \
    --toolkit-stack-name "$TOOLKIT_STACK" \
    --cloudformation-execution-policies "$EXEC_POLICY_ARN" \
    --tags project=hpcusage
echo "bootstrapped: roles cdk-$QUALIFIER-*-role-$ACCOUNT_ID-$REGION, execution policy $EXEC_POLICY_NAME"
