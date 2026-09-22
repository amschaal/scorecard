#!/usr/bin/env bash
# Build the app image and push it to the project's ECR repository (created by the base stack).
# App Runner watches the :latest tag and rolls out a new deployment on every push.
set -euo pipefail
ENV_NAME=${ENV_NAME:-prod}
export AWS_REGION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["context"].get("region", "us-west-2"))' \
                    "$(dirname "$0")/../infra/cdk.json")  # same region as the stacks
REGION=$AWS_REGION
REGISTRY="$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/hpcusage-${ENV_NAME}:latest"

aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/amd64 --target runtime -t "$IMAGE" .
docker push "$IMAGE"
