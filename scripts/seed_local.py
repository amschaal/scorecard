#!/usr/bin/env python3
"""Seed a local hpcusage stack with synthetic data so every page has something to show.

Generates sacct-format text for N days with a realistic mix of users, partitions,
GPU jobs, failures and timeouts, runs the real collector on it (--input mode), and
POSTs the envelopes. Also posts node and fairshare snapshots for each day.

    python3 scripts/seed_local.py --url http://localhost:8000 --days 45 --cluster hive
"""
import argparse
import datetime as dt
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(HERE, "..", "collector", "slurm_collector.py")
FIXTURES = os.path.join(HERE, "..", "collector", "tests", "fixtures")

USERS = [("alice", "genomics"), ("bob", "genomics"), ("carol", "climate"), ("dave", "climate"),
         ("erin", "physics"), ("frank", "physics"), ("grace", "ml-lab"), ("heidi", "ml-lab"),
         ("ivan", "stats"), ("judy", "stats"), ("mallory", "genomics"), ("oscar", "ml-lab")]
PARTITIONS = [("low", 0.35), ("med", 0.3), ("high", 0.2), ("gpu-a100", 0.15)]
STATES = [("COMPLETED", 0.78), ("FAILED", 0.1), ("TIMEOUT", 0.05), ("CANCELLED by 1000", 0.05), ("OUT_OF_MEMORY", 0.02)]


def pick(weighted):
    r, acc = random.random(), 0.0
    for item, w in weighted:
        acc += w
        if r <= acc:
            return item
    return weighted[-1][0]


def ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def gen_day(day, job_counter, n_jobs):
    """Yield sacct --parsable2 lines for jobs ENDING on `day`."""
    lines = []
    for _ in range(n_jobs):
        job_counter[0] += 1
        jid = job_counter[0]
        user, account = random.choice(USERS)
        part = pick(PARTITIONS)
        state = pick(STATES)
        gpu = part.startswith("gpu")
        cpus = random.choice([1, 2, 4, 8, 16, 32]) if not gpu else random.choice([4, 8, 16])
        nnodes = 1 if random.random() < 0.9 else random.choice([2, 4])
        mem_mb = cpus * random.choice([2000, 4000, 8000])
        elapsed = int(random.lognormvariate(8.5, 1.4))  # median ~1.4h
        elapsed = max(30, min(elapsed, 5 * 86400))
        timelimit_min = max(60, int(elapsed / 60 * random.uniform(1.05, 6)))
        if state == "TIMEOUT":
            elapsed = timelimit_min * 60
        end = dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=random.randint(0, 86399))
        start = end - dt.timedelta(seconds=elapsed)
        submit = start - dt.timedelta(seconds=int(random.expovariate(1 / 1800)))
        eff = random.betavariate(5, 2) if user not in ("mallory", "oscar") else random.betavariate(1.2, 4)
        total_cpu_s = elapsed * cpus * eff
        maxrss_k = int(mem_mb * 1024 * (random.betavariate(2, 3) if user != "mallory" else random.betavariate(1, 9)))
        gpus = random.choice([1, 2, 4]) if gpu else 0
        tres = "billing=%d,cpu=%d,mem=%dM,node=%d" % (cpus * (4 if gpu else 1), cpus, mem_mb, nnodes)
        if gpus:
            tres += ",gres/gpu=%d,gres/gpu:a100=%d" % (gpus, gpus)
        exit_code = "0:0" if state == "COMPLETED" else ("1:0" if state == "FAILED" else "0:0")
        base = [str(jid), str(jid), user, account, part, "normal", state, exit_code, ts(submit), ts(start), ts(end),
                str(elapsed), str(timelimit_min), str(cpus), str(cpus), str(nnodes), "%dM" % mem_mb, "", "1",
                "%02d:%02d:%02d" % (int(total_cpu_s) // 3600, (int(total_cpu_s) % 3600) // 60, int(total_cpu_s) % 60),
                str(elapsed * cpus), tres, tres, "c-%d-%d" % (random.randint(1, 9), random.randint(1, 20)), "None",
                random.choice(["align", "train", "sim", "qc", "assemble", "bash"])]
        lines.append("|".join(base))
        step = list(base)
        step[0] = step[1] = "%d.batch" % jid
        step[2] = step[3] = step[4] = step[5] = ""
        step[17] = "%dK" % maxrss_k
        step[-1] = "batch"
        lines.append("|".join(step))
    return lines


def run(args_list):
    subprocess.run([sys.executable, COLLECTOR] + args_list, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--token", default="dev-token-hive")
    ap.add_argument("--cluster", default="hive")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--jobs-per-day", type=int, default=400)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed)

    today = dt.date.today()
    common = ["--cluster", args.cluster, "--url", args.url, "--token", args.token,
              "--tz", "America/Los_Angeles", "--config", "/nonexistent", "--no-spool"]
    counter = [100000]
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(args.days, 0, -1):
            day = today - dt.timedelta(days=i)
            weekday_factor = 0.6 if day.weekday() >= 5 else 1.0
            n = int(args.jobs_per_day * weekday_factor * random.uniform(0.7, 1.3))
            path = os.path.join(tmp, "sacct-%s.txt" % day)
            with open(path, "w") as fh:
                fh.write("\n".join(gen_day(day, counter, n)) + "\n")
            snap_ts = (day + dt.timedelta(days=1)).strftime("%Y-%m-%dT02:15")
            print("== %s: %d jobs" % (day, n))
            run(common + ["--jobs", "--input", path, "--date", (day + dt.timedelta(days=1)).isoformat(),
                          "--days-back", "1", "--taken-at", snap_ts])
            run(common + ["--nodes", "--input", os.path.join(FIXTURES, "scontrol_node.txt"), "--taken-at", snap_ts])
            run(common + ["--fairshare", "--input", os.path.join(FIXTURES, "sshare.txt"), "--taken-at", snap_ts])
    print("done: open %s/c/%s" % (args.url, args.cluster))


if __name__ == "__main__":
    main()
