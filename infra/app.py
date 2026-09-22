#!/usr/bin/env python3
"""CDK app entry point (see infra/README.md):

    make synth       # cdk synth: templates to review in cdk.out/
    make deploy      # cdk deploy
    make push        # build + push the image; App Runner auto-deploys

Context is read from cdk.json (or -c key=value).
"""
import aws_cdk as cdk

from stacks.hpcusage_stack import HpcUsageAppStack, HpcUsageBaseStack

app = cdk.App()
env_name = app.node.try_get_context("env_name") or "prod"
# Pin the region: App Runner is not offered everywhere (us-west-1 for one), and the VPC/subnets in
# cdk.json only exist in one region. The account still comes from the credentials in use.
env = cdk.Environment(region=app.node.try_get_context("region") or "us-west-2")
base = HpcUsageBaseStack(app, f"HpcUsage-{env_name}-base", env=env,
                         description="hpcusage: VPC connector, ECR repository + push user, secrets")
HpcUsageAppStack(app, f"HpcUsage-{env_name}", base=base, env=env,
                 description="hpcusage: App Runner service")
app.synth()
