#!/usr/bin/env python3
"""CDK app entry point — run through the `cdk` compose service (see infra/README.md):

    make synth                     # offline: writes cdk.out/HpcUsage-<env>*.template.json to review
    make deploy-base / deploy-app  # sends the reviewed templates to CloudFormation, unchanged
    make push                      # code change: build + push image, App Runner auto-deploys

The stacks are environment-agnostic (no account or region baked in) and are synthesized without
the bootstrap-version rule, so the templates depend on nothing but the account they are deployed
into. Context can be set in cdk.json / cdk.context.json or with -c key=value.
"""
import aws_cdk as cdk

from stacks.hpcusage_stack import HpcUsageAppStack, HpcUsageBaseStack

app = cdk.App()
env_name = app.node.try_get_context("env_name") or "prod"
synth = lambda: cdk.DefaultStackSynthesizer(generate_bootstrap_version_rule=False)  # noqa: E731
base = HpcUsageBaseStack(app, f"HpcUsage-{env_name}-base", synthesizer=synth(),
                         description="hpcusage: VPC connector, ECR repository + push user, secrets")
HpcUsageAppStack(app, f"HpcUsage-{env_name}", base=base, synthesizer=synth(),
                 description="hpcusage: App Runner service")
app.synth()
