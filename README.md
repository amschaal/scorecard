# hpcusage — Slurm cluster usage reporting

Collects daily Slurm accounting from one or more clusters, stores it in PostgreSQL, and serves
dashboards (usage by user/account/partition, leaderboard, utilization vs. capacity, queue wait,
efficiency / "naughty list", fairshare, node state) behind UC Davis CAS.

```
 login node (scrontab, 02:15)                 AWS App Runner (or docker compose on-prem)
 ┌──────────────────────────┐   HTTPS POST    ┌────────────────────────┐      ┌────────────┐
 │ collector/               │  gzip JSON +    │ app/  FastAPI          │ SQL  │ PostgreSQL │
 │ slurm_collector.py       │  bearer token   │  ingest → upsert →     │─────▶│ (RDS or    │
 │  sacct / scontrol / sshare│ ──────────────▶│  daily rollups         │      │  local)    │
 └──────────────────────────┘                 │  Jinja2 + Plotly pages │      └────────────┘
                                              │  CAS login             │
                                              └────────────────────────┘
```

| Directory | What |
|---|---|
| [collector/](collector/) | Single stdlib-only Python 3.6+ script run by `scrontab` on each cluster. |
| [app/hpcusage/](app/hpcusage/) | FastAPI backend: ingest API, rollups, JSON API, server-rendered pages, CAS auth, Alembic migrations. |
| [infra/](infra/) | AWS CDK (Python): App Runner service + VPC connector to an existing RDS instance. |
| [scripts/](scripts/) | Token generation, local seeding, backfill, secrets/DNS helpers. |

## Quick start (local)

Requires Docker.

```bash
cp .env.example .env         # defaults are fine for local use
make up                      # Postgres + app on http://localhost:8000 (AUTH_MODE=dev, no CAS)
make seed                    # 45 days of synthetic jobs/nodes/fairshare for cluster "hive"
```

Open <http://localhost:8000>. Every page has data. `docker compose logs -f app` shows ingests.

To test the real collector against the local stack from a cluster login node:

```bash
./collector/slurm_collector.py --cluster hive --all --dry-run --output-dir /tmp/out     # inspect
./collector/slurm_collector.py --cluster hive --all --url http://<your-laptop>:8000 --token dev-token-hive
```

## Tests

```bash
make test-collector      # stdlib unittest, runs anywhere (also on the login node)
make test-app            # pytest; starts a throwaway Postgres via testcontainers (needs Docker)
make lint                # ruff
```

`app/tests/test_migrations.py` asserts the hand-written Alembic migration matches the models.

### Working inside the app container

The local image (`dev` stage) has `make`, the dev dependencies, and the whole repo bind-mounted at
`/app`, so the non-Docker targets run there too:

```bash
make shell                       # or: docker compose exec app make test
make test                        # test-app uses database hpcusage_test on the compose Postgres
make lint
make seed                        # POSTs to http://app:8000
```

Targets that need Docker (`up`, `down`, `logs`, `build`, `shell`, and the CDK targets, which run in
their own container) say so and exit if run inside the app container.

## How the data flows

1. **Collector** (`collector/slurm_collector.py`) runs at 02:15 and collects jobs whose `End`
   fell in the previous **two** full days (`[D-2, D)`), so every day is collected twice.
   Steps are folded into their parent allocation (`max(MaxRSS)`), array/het IDs are parsed,
   GPUs come from `AllocTRES`, memory formats from old and new Slurm are handled. It also
   snapshots nodes/partitions (`scontrol show node/partition --oneliner`) and fairshare (`sshare -a -P`).
2. **Ingest** (`POST /api/v1/ingest/{jobs|nodes|fairshare}`, bearer token per cluster) upserts
   per-job rows on `(cluster, job_id_raw)`, then recomputes two rollups for the affected days:
   * `daily_usage` — per (day, user, account, partition, qos): job counts by outcome, CPU/GPU/mem/node
     seconds, efficiency sums, wait sums. **Kept forever.**
   * `daily_partition_util` — allocated resource-seconds *split across the calendar days a job ran*,
     against capacity from the node snapshot; queue-wait p50/p90 for jobs started that day.
   Then it purges per-job rows older than `JOB_RETENTION_DAYS` (default 400; rollups stay).
3. **Pages** are server-rendered; charts fetch `/api/v1/...` JSON (see `/api/docs`).

### Metrics

| Page | Metrics |
|---|---|
| Cluster overview | CPU/GPU/memory hours, jobs, active users, success rate, CPU efficiency, mean wait; CPU/GPU-hours by partition; outcomes; top users/accounts; CPU utilization |
| Leaderboard | Top-N users or accounts by CPU-hours, GPU-hours, memory GB-hours, node-hours, billing-hours or jobs, with share of total |
| User / account / partition | Same KPIs scoped to the entity, usage by partition (or by user), recent jobs |
| Utilization & wait | % of capacity per partition per day (CPU/GPU/memory/nodes), queue wait p50/p90 per partition, wait histogram |
| Efficiency | CPU efficiency (`TotalCPU / elapsed×CPUs`), memory efficiency (`MaxRSS / allocated`), time-limit use, timeout rate, short-job rate, **wasted CPU-hours** — sortable, with a threshold |
| Fairshare | Current `sshare` tree with FairShare meters; trend per account/user |
| Jobs | Filterable per-job browser (within retention) |
| Nodes | Latest node/partition state, allocation, GPUs |
| Ingest | Last upload per cluster/kind with staleness flags |

## Configuration

All settings are environment variables (`app/hpcusage/settings.py`, see [.env.example](.env.example)):
`DATABASE_URL`, `APP_BASE_URL`, `SESSION_SECRET`, `AUTH_MODE` (`cas`|`dev`), `CAS_BASE`,
`CAS_VERSION`, `ALLOWED_USERS` (empty = any CAS user), `COLLECTOR_TOKENS` (JSON `{cluster: token}`),
`CLUSTERS` (JSON display metadata), `JOB_RETENTION_DAYS`, `NODE_SNAPSHOT_RETENTION_DAYS`.

### Authentication (UC Davis CAS)

CAS is handled by the [python-cas](https://github.com/python-cas/python-cas) library
(`app/hpcusage/routers/auth.py` only wires it to routes and the session cookie). `/auth/login` redirects
to `${CAS_BASE}/login?service=${APP_BASE_URL}/auth/callback?next=...`; the callback validates the
ticket (`CAS_VERSION=3` → `/p3/serviceValidate`, which also returns attributes) and stores the username
in a signed cookie. The `service` URL must be registered with IET; `APP_BASE_URL` must be exactly the
public hostname. Restrict access later by setting `ALLOWED_USERS` (or extending `hpcusage/authz.py`).
Locally, `AUTH_MODE=dev` logs everyone in as `DEV_USER`.

## Deploying to AWS

See [infra/README.md](infra/README.md). CDK and the AWS CLI run in an isolated container with no
Docker socket. Deployment is audit-then-run: `make synth` writes plain CloudFormation to
`infra/cdk.out/` offline, you review it, and `make deploy-base` / `make deploy-app` send those exact
files to CloudFormation with your own credentials (there is no CDK bootstrap). The only routine AWS
identity is a push user that can do nothing but push the image to the project's ECR repository;
`make push` builds on the host and App Runner auto-deploys `:latest`. Networking (a security group
and its rule into RDS), the custom domain, campus DNS and the CAS registration are one-time manual
steps, in that order: `make synth`, `make deploy-base`, `scripts/set_secrets.sh`, `make push`,
`make deploy-app`, `scripts/associate_domain.sh`.

## Hosting on-prem instead

Same image: `docker compose --profile tls up -d` with `HPCUSAGE_HOST=<public dns name>` in `.env`
runs Caddy in front with automatic Let's Encrypt; point `DATABASE_URL` at any PostgreSQL.

## Installing the collector on a cluster

See [collector/README.md](collector/README.md). Backfill history with
`scripts/backfill.sh <cluster> <from> <to>` (one `sacct` call per day, oldest first).

## Adding a cluster

1. Generate a token: `make token`; add it to `COLLECTOR_TOKENS` (Secrets Manager in AWS, `.env` locally) and optionally `CLUSTERS`.
2. Redeploy / restart the app.
3. Install the collector on that cluster's login node with the token in `~/.hpcusage/config.ini`.

## Notes and known limitations

* Utilization for a partition counts every node that lists that partition; overlapping partitions therefore share capacity.
* Memory efficiency for multi-node jobs is approximate (`MaxRSS` is a per-task peak); the UI marks it with ≈.
* Backfilling days older than `JOB_RETENTION_DAYS` still produces rollups, but `daily_partition_util` for those days only sees the jobs present at the time — backfill oldest-first and, if you care about split utilization, temporarily raise the retention.
* The dashboards use the categorical palette from the dataviz reference (validated for colour-vision deficiency in light and dark mode); every chart card has a **Table** toggle.
