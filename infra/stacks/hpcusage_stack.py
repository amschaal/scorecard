"""hpcusage on AWS App Runner, connected to an EXISTING RDS PostgreSQL instance.

Two stacks so the app image can be pushed between them (App Runner needs the image to exist
when the service is created):

  HpcUsage-<env>-base   VPC connector + SG rule to RDS, ECR repository, Secrets Manager secrets,
                        instance role.  `make deploy-base`
  HpcUsage-<env>        App Runner service pulling <repo>:latest with auto-deploy on push,
                        custom domain association.  `make deploy-app`

The image is built and pushed by `make push` on the host (plain docker build/push), so the
CDK toolbox container never needs a Docker socket.

Context (cdk.json or -c): env_name, vpc_id, subnet_ids (comma-separated, >= 2 AZs),
rds_security_group_id, domain, cas_base, clusters (JSON), job_retention_days, cpu, memory.

IMPORTANT: with a VPC connector attached, ALL outbound traffic from App Runner (including the
CAS ticket-validation call to cas.ucdavis.edu) goes through the VPC, so the chosen subnets need
a route to the internet (NAT gateway or IGW).
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apprunner_alpha as apprunner
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import custom_resources as cr
from constructs import Construct

IMAGE_TAG = "latest"


def _ctx(scope: Construct) -> dict:
    """Read and validate the deployment context once; shared by both stacks."""
    get = scope.node.try_get_context
    ctx = {
        "env_name": get("env_name") or "prod",
        "vpc_id": get("vpc_id"),
        "subnet_ids": [s.strip() for s in (get("subnet_ids") or "").split(",") if s.strip()],
        "rds_sg_id": get("rds_security_group_id"),
        "domain": get("domain") or None,
        "cas_base": get("cas_base") or "https://cas.ucdavis.edu/cas",
        "clusters": get("clusters") or "{}",
        "retention": str(get("job_retention_days") or "400"),
        "cpu": str(get("cpu") or "1024"),
        "memory": str(get("memory") or "2048"),
    }
    for name in ("vpc_id", "rds_sg_id"):
        if not ctx[name] or "CHANGE_ME" in ctx[name]:
            raise ValueError(f"CDK context {name!r} must be set (cdk.json or -c {name}=...)")
    if len(ctx["subnet_ids"]) < 2:
        raise ValueError("subnet_ids must list at least two subnets in different AZs")
    return ctx


class HpcUsageBaseStack(Stack):
    """Everything the service depends on but that does not depend on the image."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = _ctx(self)

        # --- networking: reach the existing RDS instance -----------------------------------
        vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=ctx["vpc_id"])
        subnets = [ec2.Subnet.from_subnet_id(self, f"Subnet{i}", sid) for i, sid in enumerate(ctx["subnet_ids"])]
        self.connector_sg = ec2.SecurityGroup(self, "ConnectorSg", vpc=vpc, allow_all_outbound=True,
                                              description="hpcusage App Runner VPC connector")
        rds_sg = ec2.SecurityGroup.from_security_group_id(self, "RdsSg", ctx["rds_sg_id"], mutable=True)
        rds_sg.add_ingress_rule(self.connector_sg, ec2.Port.tcp(5432), "hpcusage App Runner -> PostgreSQL")
        self.connector = apprunner.VpcConnector(
            self, "VpcConnector", vpc=vpc, vpc_subnets=ec2.SubnetSelection(subnets=subnets),
            security_groups=[self.connector_sg],
        )

        # --- image repository (filled by `make push`) --------------------------------------
        self.repository = ecr.Repository(
            self, "Repository", repository_name=f"hpcusage-{ctx['env_name']}",
            image_scan_on_push=True, removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[ecr.LifecycleRule(description="keep the last 20 images", max_image_count=20)],
        )

        # --- secrets (explicit names so an IAM policy can scope to hpcusage/*) -------------------
        prefix = f"hpcusage/{ctx['env_name']}"
        self.db_url_secret = sm.Secret(
            self, "DatabaseUrl", secret_name=f"{prefix}/database-url",
            description="hpcusage DATABASE_URL (postgresql://user:pass@host:5432/db)",
            secret_string_value=cdk.SecretValue.unsafe_plain_text(
                "postgresql://CHANGE_ME:CHANGE_ME@localhost:5432/hpcusage"),
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.session_secret = sm.Secret(
            self, "SessionSecret", secret_name=f"{prefix}/session-secret",
            description="hpcusage cookie-signing secret",
            generate_secret_string=sm.SecretStringGenerator(password_length=64, exclude_punctuation=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.tokens_secret = sm.Secret(
            self, "CollectorTokens", secret_name=f"{prefix}/collector-tokens",
            description='hpcusage COLLECTOR_TOKENS JSON {"cluster": "token"}',
            secret_string_value=cdk.SecretValue.unsafe_plain_text("{}"),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- runtime role (read secrets) ----------------------------------------------------------
        self.instance_role = iam.Role(self, "InstanceRole",
                                      assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"))
        for s in (self.db_url_secret, self.session_secret, self.tokens_secret):
            s.grant_read(self.instance_role)

        CfnOutput(self, "EcrRepositoryUri", value=self.repository.repository_uri,
                  description="Push the app image here (make push)")
        CfnOutput(self, "DatabaseUrlSecretArn", value=self.db_url_secret.secret_arn)
        CfnOutput(self, "CollectorTokensSecretArn", value=self.tokens_secret.secret_arn)
        CfnOutput(self, "SessionSecretArn", value=self.session_secret.secret_arn)
        CfnOutput(self, "ConnectorSecurityGroupId", value=self.connector_sg.security_group_id)


class HpcUsageAppStack(Stack):
    """The App Runner service. Requires <repository>:latest to exist (make push)."""

    def __init__(self, scope: Construct, construct_id: str, base: HpcUsageBaseStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = _ctx(self)
        self.add_dependency(base)

        app_base_url = f"https://{ctx['domain']}" if ctx["domain"] else "https://CHANGE_ME_AFTER_FIRST_DEPLOY"
        service = apprunner.Service(
            self, "Service",
            service_name=f"hpcusage-{ctx['env_name']}",
            source=apprunner.Source.from_ecr(
                repository=base.repository,
                tag_or_digest=IMAGE_TAG,
                image_configuration=apprunner.ImageConfiguration(
                    port=8000,
                    environment_variables={
                        "APP_BASE_URL": app_base_url,
                        "AUTH_MODE": "cas",
                        "CAS_BASE": ctx["cas_base"],
                        "CLUSTERS": ctx["clusters"],
                        "JOB_RETENTION_DAYS": ctx["retention"],
                        "LOG_LEVEL": "info",
                    },
                    environment_secrets={
                        "DATABASE_URL": apprunner.Secret.from_secrets_manager(base.db_url_secret),
                        "SESSION_SECRET": apprunner.Secret.from_secrets_manager(base.session_secret),
                        "COLLECTOR_TOKENS": apprunner.Secret.from_secrets_manager(base.tokens_secret),
                    },
                ),
            ),
            cpu=apprunner.Cpu.of(ctx["cpu"]),
            memory=apprunner.Memory.of(ctx["memory"]),
            instance_role=base.instance_role,
            vpc_connector=base.connector,
            auto_deployments_enabled=True,  # every `make push` to :latest rolls out a new deployment
            health_check=apprunner.HealthCheck.http(
                path="/healthz", interval=Duration.seconds(10), timeout=Duration.seconds(5),
                healthy_threshold=1, unhealthy_threshold=5,
            ),
        )

        # --- custom domain (campus DNS) ---------------------------------------------------------
        if ctx["domain"]:
            domain = ctx["domain"]
            assoc = cr.AwsCustomResource(
                self, "CustomDomain",
                on_create=cr.AwsSdkCall(
                    service="AppRunner", action="associateCustomDomain",
                    parameters={"ServiceArn": service.service_arn, "DomainName": domain, "EnableWWWSubdomain": False},
                    physical_resource_id=cr.PhysicalResourceId.of(f"{domain}@hpcusage"),
                ),
                on_delete=cr.AwsSdkCall(
                    service="AppRunner", action="disassociateCustomDomain",
                    parameters={"ServiceArn": service.service_arn, "DomainName": domain},
                ),
                policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE),
                install_latest_aws_sdk=False,
            )
            assoc.node.add_dependency(service)
            CfnOutput(self, "DnsTarget", value=assoc.get_response_field("DNSTarget"),
                      description=f"Ask campus DNS to CNAME {domain} to this hostname")
            CfnOutput(self, "DomainRecordsHint", value=f"scripts/domain_records.sh {ctx['env_name']}",
                      description="Run to print the certificate-validation CNAMEs for campus DNS")

        CfnOutput(self, "ServiceUrl", value=f"https://{service.service_url}")
        CfnOutput(self, "ServiceArn", value=service.service_arn)
