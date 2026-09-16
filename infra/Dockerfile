# Isolated toolbox for deploying hpcusage: AWS CDK CLI (Node), the Python CDK libraries and
# the AWS CLI v2. It needs AWS credentials only — no Docker socket: the app image is built and
# pushed on the host by `make push`, and the stacks deploy from ECR.
#
#   make cdk-shell                 # interactive shell in this container (cwd = infra/)
#   make synth / diff / deploy     # run cdk through it
FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AWS_PAGER="" \
    PATH="/opt/venv/bin:${PATH}" \
    CDK=cdk \
    AWS_CLI=aws

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git jq less make python3 python3-venv unzip \
    # AWS CLI v2 (official installer, picks the right architecture)
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip \
    && rm -rf /var/lib/apt/lists/*

# CDK CLI
RUN npm install -g aws-cdk@2 && npm cache clean --force

# Python CDK libraries in a venv (requirements.txt is the only build input; the code is bind-mounted)
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && pip install -r /tmp/requirements.txt

WORKDIR /workspace/infra
CMD ["bash"]
