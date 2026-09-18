# infra — AWS CDK (Python)

Deploys the hpcusage container to **AWS App Runner**, wired to an **existing RDS PostgreSQL**
instance through a VPC connector. TLS is App Runner's (ACM-managed) — no certbot.

Two stacks (`ENV_NAME`, default `prod`):

| Stack | Contents | Target |
|---|---|---|
| `HpcUsage-prod-base` | VPC connector, ECR repository + the IAM user that may push to it, Secrets Manager secrets, instance role | `make deploy-base` |
| `HpcUsage-prod` | App Runner service pulling `<repo>:latest` with auto-deploy | `make deploy-app` |

## How deployment works: audit, then run

CDK is used only to *write* CloudFormation. `make synth` runs offline — no credentials, no context
lookups, no assets — and leaves two plain templates in `infra/cdk.out/`. You read them, then the
deploy targets send **those files, unchanged** (they never re-synthesize, and print each file's
SHA-256 so you can match it to what you reviewed) to CloudFormation with your own credentials.
There is no CDK bootstrap: no `CDKToolkit` stack, bucket or roles exist in the account, and the
templates carry no CDK metadata, bootstrap-version rule, account id or region.

Everything the stacks create is named `hpcusage-*` (IAM principals under path `/hpcusage/`), and
they reference only two things they don't own, both read-only: the subnets and the security group
you created for them. Anything that touches shared resources is a one-time step you do by hand:
the connector security group and its rule into the database, the access key for the push user,
the custom-domain association, campus DNS, and the CAS service registration.

After that, the only identity that ever talks to AWS on a routine basis is the **push user**, whose
policy (in the base template) allows the five ECR actions `docker push` needs on that one repository
ARN, plus `ecr:GetAuthorizationToken` (the login token; it has no resource). A code change is
`make push`; App Runner notices the new `:latest` and rolls it out.

## Tooling: CDK and the AWS CLI run in a container

Nothing CDK- or AWS-related is installed on the host. `infra/Dockerfile` builds a toolbox with the
CDK CLI, the Python CDK libraries and the AWS CLI v2; `docker-compose.yml` runs it as the `cdk`
service (profile `cdk`, so `make up` never starts it) with:

* the repo mounted at `/workspace` (cwd `infra/`),
* the external Docker volume `hpcusage-cdk-aws` mounted at `/root/.aws`, holding the push user's
  key — outside the repo and your `~/.aws`, never removed by `docker compose down -v`. Set
  `AWS_CONFIG_DIR=~/.aws` to use your own credentials instead (the deploy targets and the one-time
  scripts need that). `AWS_PROFILE` / `AWS_REGION` are passed through (defaults `default` /
  `us-west-2`), plus `AWS_ACCESS_KEY_ID`-style variables if set in your shell.

No Docker socket is mounted. `make push` runs `docker build`/`docker push` on the host and only
fetches the ECR login password through the container.

```bash
make cdk-build                  # build the toolbox image (again after changing infra/Dockerfile or requirements.txt)
make cdk-shell                  # interactive: aws ..., cdk ..., ../scripts/*.sh
```

If you use AWS SSO, run `aws sso login --profile <name>` on the host first; the container reads the
cached token from `~/.aws`. On Linux hosts the container runs as root, so files it writes into the
bind mount (`infra/cdk.out`) will be root-owned.

## One-time setup: networking, by hand

Find the RDS network:

```bash
aws rds describe-db-instances --db-instance-identifier <id> \
  --query 'DBInstances[0].{vpc:DBSubnetGroup.VpcId,subnets:DBSubnetGroup.Subnets[].SubnetIdentifier,sgs:VpcSecurityGroups[].VpcSecurityGroupId,public:PubliclyAccessible}'
```

Then, in that VPC:

1. Create a security group for the connector, e.g. `hpcusage-prod-connector` (default egress is
   allow-all, which App Runner needs; no ingress).
2. Allow it into the database: on the RDS instance's security group, add TCP 5432 with that group
   as the source.
3. Pick **two or more subnets in different AZs that have a route to the internet** (NAT or IGW) —
   App Runner sends *all* egress through the connector, including CAS validation.

Put the results in `cdk.json`: `connector_security_group_id`, `subnet_ids`, plus `domain` and
`clusters`. The stacks never create, modify or delete a security group.

## Synthesize and review

```bash
make synth                      # infra/cdk.out/HpcUsage-prod-base.template.json and HpcUsage-prod.template.json
```

What to expect in the base template: `AWS::AppRunner::VpcConnector` (your subnets and group),
`AWS::ECR::Repository` with a 20-image lifecycle rule, `AWS::IAM::User` `hpcusage-prod-pusher` with
one `AWS::IAM::Policy`, three `AWS::SecretsManager::Secret`s (`hpcusage/prod/*`, placeholders except
the generated session secret), the instance role with an inline read policy on those secrets, and
exports for the app stack. In the app template: the ECR access role, `AWS::AppRunner::Service`
`hpcusage-prod`, and outputs. Nothing else. `cdk.out/` is gitignored; commit the reviewed templates
if you want the audited artifact in history.

`make diff` compares the synthesized templates with the deployed stacks (uses your credentials;
CDK warns that it cannot assume a bootstrap role and continues with them).

## Deploy (your credentials)

```bash
AWS_CONFIG_DIR=~/.aws make deploy-base
AWS_CONFIG_DIR=~/.aws make deploy-app       # after the image is pushed and the secrets are set
```

Each runs `aws cloudformation deploy --template-file cdk.out/<stack>.template.json` with
`CAPABILITY_NAMED_IAM` (the push user is named). App Runner's two service-linked roles
(`AWSServiceRoleForAppRunner`, `AWSServiceRoleForAppRunnerNetworking`) are created on first use by
whoever deploys, i.e. you.

## The push user

Created by the base stack (`PushUser` output). Give it a key once, and store the key in the toolbox
volume, not in the repo or your `~/.aws`:

```bash
AWS_CONFIG_DIR=~/.aws make cdk-shell
  aws iam create-access-key --user-name hpcusage-prod-pusher   # secret shown once
  exit
make cdk-shell                                # /root/.aws is now the volume
  aws configure                               # paste the key; region us-west-2; output json
  aws sts get-caller-identity                 # arn:aws:iam::<account>:user/hpcusage/hpcusage-prod-pusher
```

Rotate with `aws iam create-access-key` / `delete-access-key` (max two keys per user).
`docker volume rm hpcusage-cdk-aws` wipes the stored key. `make push` derives the repository URI
from the key's own account id, so the user needs no permission beyond its policy.

## First deployment

```bash
make synth                                  # then review infra/cdk.out/*.template.json
AWS_CONFIG_DIR=~/.aws make deploy-base
AWS_CONFIG_DIR=~/.aws make cdk-shell
  aws iam create-access-key --user-name hpcusage-prod-pusher   # -> volume, see above
  ../scripts/set_secrets.sh prod            # DATABASE_URL + collector tokens (prints the tokens)
  exit
make push                                   # host builds linux/amd64 image, pushes :latest and :<git sha>
AWS_CONFIG_DIR=~/.aws make deploy-app       # refuses while DATABASE_URL is the placeholder
AWS_CONFIG_DIR=~/.aws make cdk-shell
  ../scripts/associate_domain.sh prod       # associates the hostname, prints the CNAMEs for campus DNS
```

Then register `https://<domain>/auth/callback` as a CAS service with IET.
`scripts/domain_records.sh prod` reprints the DNS records and certificate status any time.

## Updates

* **Code change** → `make push`. App Runner watches `:latest` and rolls a new deployment
  (migrations run at container start under an advisory lock). Each push is also tagged with the
  git SHA so you can roll back by retagging.
* **Infra change** → `make synth`, review the template (or `make diff`), then
  `AWS_CONFIG_DIR=~/.aws make deploy-base` / `deploy-app` as appropriate.
* **Secret change** → `scripts/set_secrets.sh`, then `aws apprunner start-deployment --service-arn ...`
  (the script prints the command).

On Apple Silicon the image is cross-built for `linux/amd64` (App Runner is x86-only) via Docker
Desktop's buildx emulation, so expect the first build to take a few minutes.

## Removing it

Delete the push user's access keys, then the `HpcUsage-prod` and `HpcUsage-prod-base` stacks. The
ECR repository and the secrets are `RETAIN`ed and need deleting by hand, as do the security group
and its rule on the database, the DNS records and the CAS registration.

## Costs (rough)

App Runner 1 vCPU/2 GB ≈ $25–35/mo if kept warm; scales down to provisioned-container pricing
(≈ $5/mo) when idle. Secrets Manager ≈ $1.20/mo. RDS is your existing instance.
