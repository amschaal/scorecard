# Sourced by bootstrap.sh and create_deployer.sh (not executable on its own).
# Account/region plus the project-specific CDK bootstrap names, read from infra/cdk.json so the
# scripts, the IAM policies and the CDK app cannot disagree about the qualifier or stack name.
CDK_JSON="$(dirname "${BASH_SOURCE[0]}")/../infra/cdk.json"
REGION=${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
QUALIFIER=$(jq -er '.context["@aws-cdk/core:bootstrapQualifier"]' "$CDK_JSON")
TOOLKIT_STACK=$(jq -er '.toolkitStackName' "$CDK_JSON")
VPC_ID=$(jq -er '.context.vpc_id' "$CDK_JSON")
EXEC_POLICY_NAME="${QUALIFIER}-cfn-exec"
EXEC_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${EXEC_POLICY_NAME}"

# Fill the placeholders of infra/iam/*.json for this account.
render_policy() {
  sed -e "s/ACCOUNT_ID/$ACCOUNT_ID/g" -e "s/REGION/$REGION/g" -e "s/QUALIFIER/$QUALIFIER/g" \
      -e "s/TOOLKIT_STACK/$TOOLKIT_STACK/g" -e "s/VPC_ID/$VPC_ID/g" "$1"
}
