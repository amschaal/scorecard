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
* `~/.aws` mounted at `/root/.aws` (override with `AWS_CONFIG_DIR`), `AWS_PROFILE` / `AWS_REGION`
  passed through (defaults `default` / `us-west-2`), plus `AWS_ACCESS_KEY_ID`-style variables if set.

No Docker socket is mounted. `make push` runs `docker build`/`docker push` on the host and only
fetches the ECR login password through the container.

```bash
make cdk-build                  # build the toolbox image (again after changing infra/Dockerfile or requirements.txt)
make cdk-shell                  # interactive: aws sts get-caller-identity, cdk ..., ../scripts/*.sh
make bootstrap                  # once per AWS account/region
make synth / make diff
```

If you use AWS SSO, run `aws sso login --profile <name>` on the host first; the container reads the
cached token from `~/.aws`. On Linux hosts the container runs as root, so files it writes into the
bind mount (`infra/cdk.out`, `infra/cdk.context.json`) will be root-owned.

## One-time setup

Find the RDS networking facts:

```bash
aws rds describe-db-instances --db-instance-identifier <id> \
  --query 'DBInstances[0].{vpc:DBSubnetGroup.VpcId,subnets:DBSubnetGroup.Subnets[].SubnetIdentifier,sgs:VpcSecurityGroups[].VpcSecurityGroupId,public:PubliclyAccessible}'
```

Pick **two or more subnets in different AZs** of that VPC **that have a route to the internet**
(NAT or IGW) — App Runner sends *all* egress through the connector, including CAS validation.
Put the values in `cdk.json` (`vpc_id`, `subnet_ids`, `rds_security_group_id`, `domain`, `clusters`).

## First deployment

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
