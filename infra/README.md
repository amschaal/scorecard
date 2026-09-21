# infra — AWS CDK (Python)

Deploys the hpcusage container to **AWS App Runner**, wired through a VPC connector to an
**RDS PostgreSQL** instance — either one you already have (`database: existing`) or a
**db.t4g.micro the base stack creates** for this project (`database: create`). TLS is App
Runner's (ACM-managed).

| Stack | Contents |
|---|---|
| `HpcUsage-prod-base` | VPC connector, ECR repository (+ an IAM user that can only push to it), Secrets Manager secrets, instance role; with `database: create` also the RDS instance, its subnet group, master-password secret and security groups |
| `HpcUsage-prod` | App Runner service pulling `<repo>:latest`, auto-deploying on every push |

Two stacks because App Runner needs the image to exist when the service is created.

## Commands

Prerequisites on your machine: the AWS CLI, the CDK CLI (`npm install -g aws-cdk`), Docker, and the
Python CDK libraries: `pip install -r infra/requirements.txt` (in a venv if you like; `infra/.venv/`
is gitignored). The CLI must be at least as new as the library — if `cdk synth` complains about a
"cloud assembly schema version mismatch", run `npm install -g aws-cdk@latest`. Each target is the
plain command it names, run in `infra/`:

| | runs |
|---|---|
| `make synth` | `cdk synth --all` → `infra/cdk.out/HpcUsage-prod-base.template.json`, `HpcUsage-prod.template.json` |
| `make diff` | `cdk diff --all` — what a deploy would change in the account |
| `make deploy` | `cdk deploy --all` — shows IAM / security-group changes and asks before applying them |
| `make push` | `aws ecr get-login-password \| docker login`, `docker build`, `docker push` ([scripts/push.sh](../scripts/push.sh)) |
| `make bootstrap` | `cdk bootstrap` — once per account/region |

`STACKS=HpcUsage-prod-base` limits synth/diff/deploy to one stack. Your normal AWS credentials
apply (`AWS_PROFILE`, `AWS_REGION`; default region `us-west-2`). `cdk synth` needs no credentials,
so you can always review the templates before deploying; they carry no CDK metadata
(`versionReporting`/`pathMetadata` are off in `cdk.json`).

## Setup

Pick two or more subnets in different AZs **with a route to the internet** (NAT or IGW) — App
Runner sends all egress through the connector, including the CAS validation call — and put them
in `subnet_ids`. Then choose where the database comes from:

### `database: create` — the stack's own db.t4g.micro

Set `vpc_id` to the subnets' VPC. The base stack creates, in that VPC:

* the connector security group `hpcusage-prod-connector` (egress only) — or uses
  `connector_security_group_id` if you set one;
* `hpcusage-prod-db`, a security group whose only ingress is TCP 5432 from the connector group;
* a single-AZ PostgreSQL 16 `db.t4g.micro` (`db_instance_class` to change it) in `subnet_ids`,
  20 GB gp3 autoscaling to 100 GB, encrypted, 7-day backups, not publicly accessible, deletion
  protection on, snapshot on stack deletion;
* `hpcusage/prod/database-master`, a generated master password (32 alphanumeric characters), and
  fills `hpcusage/prod/database-url` with the connection string. Nothing to type in.

The subnets need not be public: App Runner reaches the instance over the connector's private
addresses. `scripts/set_secrets.sh` notices the stack-made database and only writes the tokens.

```bash
aws ec2 describe-subnets --subnet-ids subnet-a subnet-b --query 'Subnets[].[SubnetId,VpcId,AvailabilityZone]' --output table
```

### `database: existing` — a database you already run (the default)

**Network (by hand, in the RDS VPC).** Create a security group for the connector, e.g.
`hpcusage-prod-connector` (default allow-all egress, no ingress), and allow it into the database:
on the RDS instance's security group add TCP 5432 with that group as the source. The subnets
must be in the same VPC (they can be the instance's own).

```bash
aws rds describe-db-instances --db-instance-identifier <id> \
  --query 'DBInstances[0].{vpc:DBSubnetGroup.VpcId,subnets:DBSubnetGroup.Subnets[].SubnetIdentifier,sgs:VpcSecurityGroups[].VpcSecurityGroupId}'
```

**cdk.json.** Set `connector_security_group_id`, `subnet_ids`, `clusters`. In this mode
the stacks never create or modify a security group, and `scripts/set_secrets.sh` asks you for the
`DATABASE_URL`.

### Hostname and CAS

`domain` may stay empty to begin with: the service is then reached over HTTPS at its App Runner
hostname (`https://<id>.<region>.awsapprunner.com`, the `ServiceUrl` output, with an
Amazon-managed certificate) and the app builds the CAS service URL from that hostname. `cas_base`
starts at IET's development CAS (`https://ssodev.ucdavis.edu/cas`), which needs no service
registration; switch to `https://cas.ucdavis.edu/cas` when you register the app. Moving to a campus
name later is a separate step (see below) — nothing else changes.

## First deployment

```bash
make bootstrap                              # once per account/region
make synth                                  # review infra/cdk.out/*.template.json
make deploy STACKS=HpcUsage-prod-base       # ~10 min more with database=create (RDS)
scripts/set_secrets.sh prod                 # collector tokens (+ DATABASE_URL with database=existing)
make push
make deploy                                 # HpcUsage-prod (App Runner service); prints ServiceUrl
```

Open the `ServiceUrl` and log in through the development CAS.

## Moving to a campus name

1. Set `domain` (e.g. `hpcusage.ucdavis.edu`) in `cdk.json` and `make deploy` — this only sets
   `APP_BASE_URL` on the service, so logins use the new name once DNS points at it.
2. `scripts/associate_domain.sh prod` associates the name with the service and prints the records
   for campus DNS: a CNAME for the hostname to the App Runner DNS target, plus ACM validation
   CNAMEs (App Runner issues the certificate once those exist; `scripts/domain_records.sh prod`
   reprints them and the status any time).
3. Register `https://<domain>/auth/callback` as a CAS service with IET, then set `cas_base` to
   `https://cas.ucdavis.edu/cas` and `make deploy`.

The App Runner hostname keeps working alongside the custom domain.

## Updates

* **Code** → `make push`. App Runner rolls out the new `:latest` (migrations run at container
  start under an advisory lock).
* **Infra** → `make synth` (or `make diff`), review, `make deploy`.
* **Secrets** → `scripts/set_secrets.sh`, then `aws apprunner start-deployment --service-arn ...`
  (the script prints the command).

On Apple Silicon the image is cross-built for `linux/amd64` (App Runner is x86-only) via Docker
Desktop's buildx emulation; the first build takes a few minutes.

## Pushing from somewhere else

The base stack creates `hpcusage-prod-pusher`, an IAM user whose only permissions are the ECR
actions `docker push` needs on this repository. For CI or a colleague: `aws iam create-access-key
--user-name hpcusage-prod-pusher`, put the key in a profile, and run `AWS_PROFILE=<name> make push`.

## Removing it

`cd infra && cdk destroy --all`, then by hand: the ECR repository and the secrets
(both `RETAIN`ed), the DNS records and the CAS registration. With `database: existing`, also the
security group and its rule on the database. With `database: create`, first turn off deletion
protection (`aws rds modify-db-instance --db-instance-identifier hpcusage-prod --no-deletion-protection`);
the destroy then takes a final snapshot of the instance and leaves it behind.

Switching an existing deployment from `existing` to `create` redeploys the base stack with the
new instance and overwrites the database-url secret; the data does not move. Dump/restore it
yourself (`pg_dump` from inside the VPC, using the master secret) or backfill from the clusters.

## Costs (rough)

App Runner 1 vCPU/2 GB ≈ $25–35/mo if kept warm; scales down to provisioned-container pricing
(≈ $5/mo) when idle. Secrets Manager ≈ $1.20/mo (+ $0.40 for the master secret with
`database: create`). The stack's `db.t4g.micro` is ≈ $12/mo plus gp3 storage (≈ $2.30/mo for
20 GB) and backups; with `database: existing` RDS is whatever you already pay.
