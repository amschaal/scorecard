"""hpcusage on AWS App Runner, connected to an RDS PostgreSQL instance: either one that already
exists (context database=existing, the default) or a db.t4g.micro the base stack creates for
this project (database=create).

Two stacks so the app image can be pushed between them (App Runner needs the image to exist
when the service is created):

  HpcUsage-<env>-base   VPC connector, ECR repository + the IAM user that may push to it,
                        Secrets Manager secrets, instance role.
  HpcUsage-<env>        App Runner service pulling <repo>:latest with auto-deploy on push.

`cdk synth` needs no credentials (no context lookups, no assets), so the templates in cdk.out/
can be reviewed before `cdk deploy`. The image is built and pushed by `make push` (scripts/push.sh).

Context (cdk.json or -c): env_name, subnet_ids (comma-separated, >= 2 AZs),
connector_security_group_id, domain, cas_base, clusters (JSON), job_retention_days, cpu, memory,
database (existing|create), vpc_id, db_instance_class.

database=existing: networking stays outside the stacks on purpose. You create the connector
security group in the RDS VPC and allow it into the database yourself (infra/README.md); the
stacks only reference its id and you put the connection string in the database-url secret.

database=create: the base stack creates the connector security group (unless
connector_security_group_id is set), a db.t4g.micro PostgreSQL instance in `subnet_ids` with a
security group that admits only the connector, a generated master password, and fills the
database-url secret itself. Needs vpc_id.

`domain` is optional: unset, the service is reached over HTTPS on its App Runner hostname
(ServiceUrl output) and the app takes APP_BASE_URL from each request. Set it later to move to a
campus name: redeploy, then associate the domain once by hand (scripts/associate_domain.sh).

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
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

IMAGE_TAG = "latest"
IAM_PATH = "/hpcusage/"  # every IAM principal of the project, easy to find and to scope policies to
DB_NAME = DB_USER = "hpcusage"
DB_PORT = 5432


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
        "database": get("database") or "existing",
        "vpc_id": get("vpc_id") or None,
        "db_instance_class": get("db_instance_class") or "db.t4g.micro",
    }
    if ctx["database"] not in ("existing", "create"):
        raise ValueError("CDK context 'database' must be 'existing' or 'create'")
    if ctx["connector_sg_id"] and "CHANGE_ME" in ctx["connector_sg_id"]:
        ctx["connector_sg_id"] = None
    if ctx["database"] == "existing" and not ctx["connector_sg_id"]:
        raise ValueError("CDK context 'connector_security_group_id' must be set (cdk.json or -c ...)")
    if ctx["database"] == "create" and (not ctx["vpc_id"] or "CHANGE_ME" in ctx["vpc_id"]):
        raise ValueError("CDK context 'vpc_id' must be set when database=create")
    if len(ctx["subnet_ids"]) < 2 or any("CHANGE_ME" in s for s in ctx["subnet_ids"]):
        raise ValueError("subnet_ids must list at least two subnets in different AZs")
    return ctx


class HpcUsageBaseStack(Stack):
    """Everything the service depends on but that does not depend on the image."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = _ctx(self)
        env_name = ctx["env_name"]

        # --- networking: reach the RDS instance ------------------------------------------------
        # L1 on purpose: the L2 VpcConnector needs a VPC object, which means either a context
        # lookup (credentials at synth time) or listing availability zones by hand.
        connector_name = f"hpcusage-{env_name}"
        connector_sg_id = ctx["connector_sg_id"]
        if connector_sg_id is None:  # database=create without an existing group: make one
            connector_sg_id = ec2.CfnSecurityGroup(
                self, "ConnectorSecurityGroup", vpc_id=ctx["vpc_id"], group_name=f"{connector_name}-connector",
                group_description="hpcusage App Runner VPC connector (egress only)",
            ).attr_group_id
        cfn_connector = apprunner_cfn.CfnVpcConnector(
            self, "VpcConnector", vpc_connector_name=connector_name,
            subnets=ctx["subnet_ids"], security_groups=[connector_sg_id],
        )
        self.connector = apprunner.VpcConnector.from_vpc_connector_attributes(
            self, "Connector",
            vpc_connector_arn=cfn_connector.attr_vpc_connector_arn, vpc_connector_name=connector_name,
            vpc_connector_revision=cfn_connector.attr_vpc_connector_revision,
            security_groups=[ec2.SecurityGroup.from_security_group_id(self, "ConnectorSg", connector_sg_id)],
        )

        # --- the database itself, when asked for -------------------------------------------------
        database_url = cdk.SecretValue.unsafe_plain_text(
            f"postgresql://CHANGE_ME:CHANGE_ME@localhost:{DB_PORT}/{DB_NAME}")
        if ctx["database"] == "create":
            database_url = self._create_database(ctx, connector_sg_id)

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
            secret_string_value=database_url,
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
        CfnOutput(self, "ConnectorSecurityGroupId", value=connector_sg_id,
                  description="Allow this group into the database (TCP 5432) if the database is not the stack's")

    def _create_database(self, ctx: dict, connector_sg_id: str) -> cdk.SecretValue:
        """A single-AZ PostgreSQL instance reachable only from the VPC connector. Returns the
        DATABASE_URL for the app, resolved by CloudFormation at deploy time (the password never
        appears in the template, only a {{resolve:secretsmanager:...}} reference to it)."""
        env_name = ctx["env_name"]
        db_sg = ec2.CfnSecurityGroup(
            self, "DatabaseSecurityGroup", vpc_id=ctx["vpc_id"], group_name=f"hpcusage-{env_name}-db",
            group_description="hpcusage RDS instance: PostgreSQL from the App Runner VPC connector only",
            security_group_ingress=[ec2.CfnSecurityGroup.IngressProperty(
                ip_protocol="tcp", from_port=DB_PORT, to_port=DB_PORT, source_security_group_id=connector_sg_id,
                description="hpcusage App Runner VPC connector",
            )],
        )
        subnet_group = rds.CfnDBSubnetGroup(
            self, "DatabaseSubnetGroup", db_subnet_group_name=f"hpcusage-{env_name}",
            db_subnet_group_description="hpcusage RDS subnets (the VPC connector's)", subnet_ids=ctx["subnet_ids"],
        )
        # Alphanumeric only: RDS forbids / @ " and space, and the URL below would need escaping.
        master = sm.Secret(
            self, "DatabaseMaster", secret_name=f"hpcusage/{env_name}/database-master",
            description=f"hpcusage RDS master credentials (user {DB_USER})",
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template=f'{{"username": "{DB_USER}"}}', generate_string_key="password",
                password_length=32, exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        password = master.secret_value_from_json("password")
        db = rds.CfnDBInstance(
            self, "Database", db_instance_identifier=f"hpcusage-{env_name}",
            engine="postgres", engine_version="16", db_instance_class=ctx["db_instance_class"],
            db_name=DB_NAME, master_username=DB_USER, master_user_password=password.unsafe_unwrap(),
            port=str(DB_PORT), db_subnet_group_name=subnet_group.ref, vpc_security_groups=[db_sg.attr_group_id],
            publicly_accessible=False, multi_az=False,
            allocated_storage="20", max_allocated_storage=100, storage_type="gp3", storage_encrypted=True,
            backup_retention_period=7, copy_tags_to_snapshot=True, auto_minor_version_upgrade=True,
            deletion_protection=True,
        )
        db.apply_removal_policy(RemovalPolicy.SNAPSHOT)
        # Adds host/port/dbname/engine to the master secret so the console and `aws rds` tooling can use it.
        sm.CfnSecretTargetAttachment(self, "DatabaseMasterAttachment", secret_id=master.secret_arn,
                                     target_id=db.ref, target_type="AWS::RDS::DBInstance")

        CfnOutput(self, "DatabaseEndpoint", value=f"{db.attr_endpoint_address}:{db.attr_endpoint_port}",
                  description="The stack's RDS instance; reachable only through the VPC connector")
        CfnOutput(self, "DatabaseMasterSecretArn", value=master.secret_arn,
                  description="Master credentials for psql (from inside the VPC)")
        return cdk.SecretValue.unsafe_plain_text(
            f"postgresql://{DB_USER}:{password.unsafe_unwrap()}@{db.attr_endpoint_address}:{db.attr_endpoint_port}"
            f"/{DB_NAME}?sslmode=require")


class HpcUsageAppStack(Stack):
    """The App Runner service. Requires <repository>:latest to exist (make push)."""

    def __init__(self, scope: Construct, construct_id: str, base: HpcUsageBaseStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = _ctx(self)
        self.add_dependency(base)

        # Without a custom domain the app answers on its App Runner hostname and derives APP_BASE_URL
        # from each request (the hostname is not known until the service exists).
        env_vars = {
            "AUTH_MODE": "cas",
            "CAS_BASE": ctx["cas_base"],
            "CLUSTERS": ctx["clusters"],
            "JOB_RETENTION_DAYS": ctx["retention"],
            "LOG_LEVEL": "info",
        }
        if ctx["domain"]:
            env_vars["APP_BASE_URL"] = f"https://{ctx['domain']}"
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
                    environment_variables=env_vars,
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
