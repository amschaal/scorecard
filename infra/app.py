#!/usr/bin/env python3
"""CDK app entry point — run through the `cdk` compose service (see infra/README.md):

    make bootstrap                 # once per account/region
    make deploy                    # = deploy-base -> push -> deploy-app
    make push                      # code change: build + push image, App Runner auto-deploys

Context can be set in cdk.json / cdk.context.json or with -c key=value.
"""
import os

import aws_cdk as cdk

from stacks.hpcusage_stack import HpcUsageAppStack, HpcUsageBaseStack

app = cdk.App()
env_name = app.node.try_get_context("env_name") or "prod"
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-west-2"),
)
base = HpcUsageBaseStack(app, f"HpcUsage-{env_name}-base", env=env,
                         description="hpcusage: VPC connector, ECR repository, secrets")
HpcUsageAppStack(app, f"HpcUsage-{env_name}", base=base, env=env,
                 description="hpcusage: App Runner service")
app.synth()
