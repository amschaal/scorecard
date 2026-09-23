# hpcusage collector

Single-file, standard-library-only Python (3.6+) script that runs on a cluster
login node and ships Slurm accounting to the hpcusage backend.

```
slurm_collector.py --all                 # jobs (last 2 full days) + nodes + fairshare
slurm_collector.py --jobs --date 2026-09-01 --days-back 30   # backfill August
slurm_collector.py --jobs --dry-run --output-dir /tmp/out    # inspect without posting
slurm_collector.py --jobs --input sacct.txt --date 2026-09-15 --days-back 1 --dry-run --output-dir /tmp/out
```

## Install on a cluster

```
mkdir -p ~/hpcusage ~/.hpcusage
cp slurm_collector.py ~/hpcusage/
cp config.ini.example ~/.hpcusage/config.ini && chmod 600 ~/.hpcusage/config.ini
$EDITOR ~/.hpcusage/config.ini          # url, token, cluster, timezone
~/hpcusage/slurm_collector.py --all --dry-run --output-dir /tmp/hpcusage-test   # smoke test
scrontab scrontab.example                # after editing USERNAME
```

## What it runs

| Kind | Command |
|---|---|
| jobs | `sacct --allusers --parsable2 --noheader --noconvert -S <day 00:00> -E <next day 00:00> --format=...` one call per day |
| nodes | `scontrol show node --oneliner` + `scontrol show partition --oneliner` (falls back to `sinfo -N`) |
| fairshare | `sshare -a -P --noheader --format=Account,User,RawShares,...` |

Jobs are kept if their `End` falls inside the window and their state is
terminal. Step rows (`.batch`, `.extern`, `.N`) are folded into the parent
allocation (max `MaxRSS`, summed `NTasks`). Running jobs are collected on the
day they finish.

Failed uploads are retried three times, then spooled to `~/.hpcusage/spool/`
and resent on the next run. To drain the spool without collecting anything new
(for example after fixing the backend), run:

```
python3 slurm_collector.py --resend
```

It re-POSTs each spooled envelope once, oldest first, deletes the ones that
succeed, and exits 1 if any remain. Job envelopes are upserts and safe to
resend; node and fairshare envelopes insert a snapshot at their recorded time,
so resend those only if that snapshot is missing.

## Tests

```
python3 -m unittest discover -s collector/tests -v
```
