#!/usr/bin/env bash
# Create the project-scoped IAM user that the cdk toolbox uses, with infra/iam/deployer-policy.json
# as an inline policy, and print an access key to paste into `aws configure` inside the toolbox.
#
# Run ONCE with your own admin credentials, e.g.:
#   AWS_CONFIG_DIR=~/.aws make cdk-shell
#   ../scripts/create_deployer.sh [user-name]
#
# Env: AWS_REGION (default us-west-2), CDK_QUALIFIER (default hnb659fds — change only if you
# bootstrapped with --qualifier).
set -euo pipefail
USER_NAME=${1:-hpcusage-deployer}
REGION=${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}
QUALIFIER=${CDK_QUALIFIER:-hnb659fds}
POLICY_FILE="$(dirname "$0")/../infra/iam/deployer-policy.json"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "account $ACCOUNT_ID, region $REGION, bootstrap qualifier $QUALIFIER"
policy=$(sed -e "s/ACCOUNT_ID/$ACCOUNT_ID/g" -e "s/REGION/$REGION/g" -e "s/QUALIFIER/$QUALIFIER/g" "$POLICY_FILE")

if aws iam get-user --user-name "$USER_NAME" >/dev/null 2>&1; then
  echo "IAM user $USER_NAME already exists; updating its policy"
else
  aws iam create-user --user-name "$USER_NAME" --tags Key=project,Value=hpcusage >/dev/null
  echo "created IAM user $USER_NAME"
fi
aws iam put-user-policy --user-name "$USER_NAME" --policy-name hpcusage-deployer --policy-document "$policy"
echo "attached inline policy hpcusage-deployer"

n_keys=$(aws iam list-access-keys --user-name "$USER_NAME" --query 'length(AccessKeyMetadata)' --output text)
if [[ "$n_keys" -ge 2 ]]; then
  echo "user already has 2 access keys; delete one with 'aws iam delete-access-key' before creating another" >&2
  exit 1
fi
key=$(aws iam create-access-key --user-name "$USER_NAME" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
read -r AKID SECRET <<<"$key"

cat <<MSG

Access key created. Store it in the toolbox volume (NOT in the repo or your host ~/.aws):

  make cdk-shell            # without AWS_CONFIG_DIR, so /root/.aws is the hpcusage-cdk-aws volume
  aws configure
     AWS Access Key ID:     $AKID
     AWS Secret Access Key: $SECRET
     Default region name:   $REGION
     Default output format: json
  aws sts get-caller-identity     # should show user/$USER_NAME

The secret is shown only once. Rotate later with: aws iam create-access-key / delete-access-key.
MSG
