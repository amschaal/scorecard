"""hpcusage on AWS App Runner, connected to an EXISTING RDS PostgreSQL instance.

Two stacks so the app image can be pushed between them (App Runner needs the image to exist
when the service is created):

  HpcUsage-<env>-base   VPC connector, ECR repository + the IAM user that may push to it,
                        Secrets Manager secrets, instance role.  `make deploy-base`
  HpcUsage-<env>        App Runner service pulling <repo>:latest with auto-deploy on push.
                        `make deploy-app`

`cdk synth` runs offline (no credentials, no context lookups, no assets) and writes plain
CloudFormation to cdk.out/ for review; the deploy targets send those files, unchanged, to
CloudFormation with your own credentials. There is no CDK bootstrap. The image is built and
pushed by `make push` on the host with the push user's key, so the toolbox container never
needs a Docker socket.

Context (cdk.json or -c): env_name, subnet_ids (comma-separated, >= 2 AZs),
connector_security_group_id, domain, cas_base, clusters (JSON), job_retention_days, cpu, memory.

Networking stays outside the stacks on purpose: you create the connector security group in the
RDS VPC and allow it into the database yourself (infra/README.md); the stacks only reference its
id. The custom domain is associated once by hand (scripts/associate_domain.sh).

IMPORTANT: with a VPC connector attached, ALL outbound traffic from App Runner (including the
CAS ticket-validation call to cas.ucdavis.edu) goes through the VPC, so the chosen subnets need
a route to the internet (NAT gateway or IGW).
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apprunner as apprunner_cfn
from aws_cdk import aws_apprunner_alpha as apprunner
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

IMAGE_TAG = "latest"
IAM_PATH = "/hpcusage/"  # every IAM principal of the project, easy to find and to scope policies to


def _ctx(scope: Construct) -> dict:
    """Read and validate the deployment context once; shared by both stacks."""
    get = scope.node.try_get_context
    ctx = {
        "env_name": get("env_name") or "prod",
        "subnet_ids": [s.strip() for s in (get("subnet_ids") or "").split(",") if s.strip()],
        "connector_sg_id": get("connector_security_group_id"),
        "domain": get("domain") or None,
        "cas_base": get("cas_base") or "https://cas.ucdavis.edu/cas",
        "clusters": get("clusters") or "{}",
        "retention": str(get("job_retention_days") or "400"),
        "cpu": str(get("cpu") or "1024"),
        "memory": str(get("memory") or "2048"),
    }
    if not ctx["connector_sg_id"] or "CHANGE_ME" in ctx["connector_sg_id"]:
        raise ValueError("CDK context 'connector_security_group_id' must be set (cdk.json or -c ...)")
    if len(ctx["subnet_ids"]) < 2 or any("CHANGE_ME" in s for s in ctx["subnet_ids"]):
        raise ValueError("subnet_ids must list at least two subnets in different AZs")
    return ctx


class HpcUsageBaseStack(Stack):
    """Everything the service depends on but that does not depend on the image."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = _ctx(self)
        env_name = ctx["env_name"]

        # --- networking: reach the existing RDS instance -----------------------------------
        # L1 on purpose: the L2 VpcConnector needs a VPC object, which means either a context
        # lookup (credentials at synth time) or listing availability zones by hand.
        connector_name = f"hpcusage-{env_name}"
        cfn_connector = apprunner_cfn.CfnVpcConnector(
            self, "VpcConnector", vpc_connector_name=connector_name,
            subnets=ctx["subnet_ids"], security_groups=[ctx["connector_sg_id"]],
        )
        self.connector = apprunner.VpcConnector.from_vpc_connector_attributes(
            self, "Connector",
            vpc_connector_arn=cfn_connector.attr_vpc_connector_arn, vpc_connector_name=connector_name,
            vpc_connector_revision=cfn_connector.attr_vpc_connector_revision,
            security_groups=[ec2.SecurityGroup.from_security_group_id(self, "ConnectorSg", ctx["connector_sg_id"])],
        )

        # --- image repository, and the only identity that can push to it -----------------------
        self.repository = ecr.Repository(
            self, "Repository", repository_name=f"hpcusage-{env_name}",
            image_scan_on_push=True, removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[ecr.LifecycleRule(description="keep the last 20 images", max_image_count=20)],
        )
        pusher = iam.User(self, "Pusher", user_name=f"hpcusage-{env_name}-pusher", path=IAM_PATH)
        iam.Policy(self, "PusherPolicy", policy_name="push-image", users=[pusher], statements=[
            iam.PolicyStatement(  # the documented minimum for `docker push`, on this repository only
                actions=["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload",
                         "ecr:PutImage", "ecr:UploadLayerPart"],
                resources=[self.repository.repository_arn],
            ),
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]),  # login token; has no resource
        ])

        # --- secrets (explicit names so an IAM policy can scope to hpcusage/*) -------------------
        prefix = f"hpcusage/{env_name}"
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
        self.instance_role = iam.Role(self, "InstanceRole", path=IAM_PATH,
                                      assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"))
        for s in (self.db_url_secret, self.session_secret, self.tokens_secret):
            s.grant_read(self.instance_role)

        CfnOutput(self, "EcrRepositoryUri", value=self.repository.repository_uri,
                  description="Push the app image here (make push)")
        CfnOutput(self, "PushUser", value=pusher.user_name,
                  description="May only push to the repository; create its key with aws iam create-access-key")
        CfnOutput(self, "DatabaseUrlSecretArn", value=self.db_url_secret.secret_arn)
        CfnOutput(self, "CollectorTokensSecretArn", value=self.tokens_secret.secret_arn)
        CfnOutput(self, "SessionSecretArn", value=self.session_secret.secret_arn)


class HpcUsageAppStack(Stack):
    """The App Runner service. Requires <repository>:latest to exist (make push)."""

    def __init__(self, scope: Construct, construct_id: str, base: HpcUsageBaseStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = _ctx(self)
        self.add_dependency(base)

        app_base_url = f"https://{ctx['domain']}" if ctx["domain"] else "https://CHANGE_ME_AFTER_FIRST_DEPLOY"
        # Explicit (instead of construct-generated) so it sits under IAM_PATH; pulls the image from ECR.
        access_role = iam.Role(self, "AccessRole", path=IAM_PATH,
                               assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"))
        base.repository.grant_pull(access_role)
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
            access_role=access_role,
            vpc_connector=base.connector,
            auto_deployments_enabled=True,  # every `make push` to :latest rolls out a new deployment
            health_check=apprunner.HealthCheck.http(
                path="/healthz", interval=Duration.seconds(10), timeout=Duration.seconds(5),
                healthy_threshold=1, unhealthy_threshold=5,
            ),
        )

        CfnOutput(self, "ServiceUrl", value=f"https://{service.service_url}")
        CfnOutput(self, "ServiceArn", value=service.service_arn)
        if ctx["domain"]:
            CfnOutput(self, "CustomDomainHint", value=f"scripts/associate_domain.sh {ctx['env_name']}",
                      description=f"Run once to associate {ctx['domain']} and print the CNAMEs for campus DNS")
