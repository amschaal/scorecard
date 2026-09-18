# infra — AWS CDK (Python)

Deploys the hpcusage container to **AWS App Runner**, wired to an **existing RDS PostgreSQL**
instance through a VPC connector. TLS is App Runner's (ACM-managed) — no certbot.

Two stacks (`ENV_NAME`, default `prod`):

| Stack | Contents | Target |
|---|---|---|
| `HpcUsage-prod-base` | VPC connector + SG rule into the RDS security group, ECR repository, Secrets Manager secrets, instance role | `make deploy-base` |
| `HpcUsage-prod` | App Runner service pulling `<repo>:latest` with auto-deploy, custom-domain association | `make deploy-app` |

The app image is **built and pushed by the host** (`make push`, plain `docker build`/`push`), and the
service deploys from ECR — so CDK never needs a Docker socket, and after the first deployment a
code change is just `make push` (App Runner notices the new `:latest` and rolls it out).

## Tooling: CDK and the AWS CLI run in a container

Nothing CDK- or AWS-related is installed on the host. `infra/Dockerfile` builds a toolbox with the
CDK CLI, the Python CDK libraries and the AWS CLI v2; `docker-compose.yml` runs it as the `cdk`
service (profile `cdk`, so `make up` never starts it) with:

* the repo mounted at `/workspace` (cwd `infra/`),
* the external Docker volume `hpcusage-cdk-aws` mounted at `/root/.aws`, holding the project-scoped
  credentials (next section). Set `AWS_CONFIG_DIR=~/.aws` to use your host credentials instead.
  `AWS_PROFILE` / `AWS_REGION` are passed through (defaults `default` / `us-west-2`), plus
  `AWS_ACCESS_KEY_ID`-style variables if set in your shell.

No Docker socket is mounted. `make push` runs `docker build`/`docker push` on the host and only
fetches the ECR login password through the container.

## One-time setup: point cdk.json at the RDS network

Find the RDS networking facts:

```bash
aws rds describe-db-instances --db-instance-identifier <id> \
  --query 'DBInstances[0].{vpc:DBSubnetGroup.VpcId,subnets:DBSubnetGroup.Subnets[].SubnetIdentifier,sgs:VpcSecurityGroups[].VpcSecurityGroupId,public:PubliclyAccessible}'
```

Pick **two or more subnets in different AZs** of that VPC **that have a route to the internet**
(NAT or IGW) — App Runner sends *all* egress through the connector, including CAS validation.
Put the values in `cdk.json` (`vpc_id`, `subnet_ids`, `rds_security_group_id`, `domain`, `clusters`).
Do this before bootstrapping: the bootstrap's execution policy is pinned to `vpc_id`.

## Project-scoped AWS access

Nothing this project runs can touch anything else in the account. Two layers:

### 1. A private CDK bootstrap (`make bootstrap`)

[scripts/bootstrap.sh](../scripts/bootstrap.sh) does not use the account-wide default bootstrap
(stack `CDKToolkit`, qualifier `hnb659fds`, `AdministratorAccess` execution role). It creates a
separate bootstrap stack **`CDKToolkit-hpcusage`** with qualifier **`hpcusage`** (both set in
`cdk.json`), so its roles and bucket are `cdk-hpcusage-*` and it cannot collide with any other CDK
project in the account. Its CloudFormation execution role — the identity that actually creates and
deletes resources during `cdk deploy` — carries the customer-managed policy `hpcusage-cfn-exec`,
rendered from [iam/cfn-exec-policy.json](iam/cfn-exec-policy.json), instead of `AdministratorAccess`.
That policy allows:

| Resource | Scope |
|---|---|
| App Runner services and VPC connectors | named `hpcusage-*` |
| ECR repositories | named `hpcusage-*` |
| Secrets Manager secrets | named `hpcusage/*` |
| Lambda (the custom-domain custom resource) | named `hpcusage-*` |
| IAM roles (create/delete, inline policies, `PassRole`) | under path `/hpcusage/`; the only attachable managed policy is `AWSLambdaBasicExecutionRole`; service-linked roles for App Runner only |
| Security groups | create in, and add/remove rules on groups in, the RDS VPC (`vpc_id`) — this is the one place a name pattern cannot apply, since the ingress rule goes on the *existing* RDS group |
| CDK assets bucket | read `cdk-hpcusage-assets-*` (Lambda code) |

Within each of those scopes the actions are broad (`apprunner:*` on `service/hpcusage-*`, …):
the isolation is by resource name, which is what keeps the policy stable across CDK upgrades.
The stacks, in turn, name every resource `hpcusage-*` or put it under `/hpcusage/` (see `IAM_PATH`
in `stacks/hpcusage_stack.py`) — keep new resources on the same pattern or the execution role will
be denied. If a deploy fails with `AccessDenied`, the CloudFormation event names the action and
resource: add it to `cfn-exec-policy.json` and re-run `make bootstrap` (it updates the policy in
place and is otherwise idempotent).

### 2. A project-scoped deployer user

The toolbox does not use your personal AWS credentials day to day. Instead a dedicated IAM user
(`hpcusage-deployer`) with the inline policy in [iam/deployer-policy.json](iam/deployer-policy.json)
lives in the `hpcusage-cdk-aws` Docker volume — outside the repo and outside your `~/.aws`, and never
removed by `docker compose down -v` (it is declared `external`).

What the policy allows, and nothing else:

| Purpose | Permission |
|---|---|
| `cdk deploy` / `diff` / VPC lookup | `sts:AssumeRole` on the bootstrap roles `cdk-hpcusage-*-role-<account>-<region>`; read the bootstrap version parameter |
| `make push` | `ecr:GetAuthorizationToken` + push/pull on repositories `hpcusage-*` |
| helper scripts | `cloudformation:DescribeStacks` on `HpcUsage-*` / `CDKToolkit-hpcusage`, read/write secrets `hpcusage/*`, App Runner `StartDeployment` / `DescribeCustomDomains` on services `hpcusage-*` |

So the deployer keys can only start deployments, and deployments can only touch project resources.

### Setting it up

Two steps need your own (admin) credentials, once (`cdk.json` must already have `vpc_id`):

```bash
AWS_CONFIG_DIR=~/.aws make bootstrap          # policy hpcusage-cfn-exec + stack CDKToolkit-hpcusage
AWS_CONFIG_DIR=~/.aws make cdk-shell
  ../scripts/create_deployer.sh               # IAM user + policy; prints an access key (shown once)
  exit
```

Then store the key in the volume and use it from now on:

```bash
make cdk-shell                                # /root/.aws is the volume
  aws configure                               # paste the key; region us-west-2; output json
  aws sts get-caller-identity                 # arn:aws:iam::<account>:user/hpcusage-deployer
```

Rotate with `aws iam create-access-key` / `delete-access-key` (max two keys per user) and re-run
`aws configure`. `docker volume rm hpcusage-cdk-aws` wipes the stored key.

To remove the project from the account entirely: `cdk destroy --all`, delete the
`CDKToolkit-hpcusage` stack (empty its `cdk-hpcusage-assets-*` bucket first), the `hpcusage-cfn-exec`
policy and the `hpcusage-deployer` user. The ECR repository and secrets are `RETAIN`ed by the
stacks and need deleting by hand.

```bash
make cdk-build                  # build the toolbox image (again after changing infra/Dockerfile or requirements.txt)
make cdk-shell                  # interactive: aws sts get-caller-identity, cdk ..., ../scripts/*.sh
make bootstrap                  # once per AWS account/region (admin credentials)
make synth / make diff
```

If you use AWS SSO, run `aws sso login --profile <name>` on the host first; the container reads the
cached token from `~/.aws`. On Linux hosts the container runs as root, so files it writes into the
bind mount (`infra/cdk.out`, `infra/cdk.context.json`) will be root-owned.

## First deployment

With the deployer credentials in place (previous section):

```bash
make deploy-base                            # VPC connector, ECR repo, secrets
make cdk-shell                              # then, inside the container:
  ../scripts/set_secrets.sh prod            #   DATABASE_URL + collector tokens (prints the tokens)
  exit
make push                                   # host builds linux/amd64 image, pushes :latest and :<git sha>
make deploy-app                             # App Runner service (refuses while DATABASE_URL is the placeholder)
make cdk-shell
  ../scripts/domain_records.sh prod         # CNAMEs to request from campus DNS
```

`make deploy` runs the three CDK/push steps in order (use it once the secrets are set). Then
register `https://<domain>/auth/callback` as a CAS service with IET.

## Updates

* **Code change** → `make push`. App Runner watches `:latest` and rolls a new deployment
  (migrations run at container start under an advisory lock). Each push is also tagged with the
  git SHA so you can roll back by retagging.
* **Infra change** → `make diff`, then `make deploy-base` / `make deploy-app` as appropriate.
* **Secret change** → `scripts/set_secrets.sh`, then `aws apprunner start-deployment --service-arn ...`
  (the script prints the command).

On Apple Silicon the image is cross-built for `linux/amd64` (App Runner is x86-only) via Docker
Desktop's buildx emulation, so expect the first build to take a few minutes.

## Costs (rough)

App Runner 1 vCPU/2 GB ≈ $25–35/mo if kept warm; scales down to provisioned-container pricing
(≈ $5/mo) when idle. Secrets Manager ≈ $1.20/mo. RDS is your existing instance.
