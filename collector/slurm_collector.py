#!/usr/bin/env python3
"""
hpcusage Slurm collector.

Collects daily job accounting (sacct), node/partition capacity (scontrol) and
fairshare (sshare) from a Slurm cluster and POSTs gzipped JSON envelopes to the
hpcusage web backend.

Design constraints:
  * Python 3.6+ and standard library only (runs on cluster login nodes).
  * Idempotent: the backend upserts on (cluster, job_id_raw), so re-running a
    window is always safe. The default window covers the last TWO full days so
    every day is collected twice, which covers slurmdbd lag and a missed run.
  * Never loads sacct output as one big string; lines are streamed.

Typical scrontab entry (see scrontab.example):
    15 2 * * * ~/hpcusage/slurm_collector.py --all

Configuration is read from ~/.hpcusage/config.ini (or --config), e.g.:
    [hpcusage]
    url = https://hpcusage.ucdavis.edu
    token = <per-cluster bearer token>
    cluster = hive
    timezone = America/Los_Angeles
Command-line flags override the config file.
"""

import argparse
import configparser
import datetime as dt
import gzip
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

COLLECTOR_VERSION = "0.1.0"

DEFAULT_CONFIG = os.path.expanduser("~/.hpcusage/config.ini")
DEFAULT_SPOOL = os.path.expanduser("~/.hpcusage/spool")

# Order matters: JobName is last so a '|' inside a job name cannot shift columns.
SACCT_FIELDS = [
    "JobIDRaw", "JobID", "User", "Account", "Partition", "QOS", "State", "ExitCode",
    "Submit", "Start", "End", "ElapsedRaw", "TimelimitRaw", "AllocCPUS", "ReqCPUS",
    "NNodes", "ReqMem", "MaxRSS", "NTasks", "TotalCPU", "CPUTimeRAW", "AllocTRES",
    "ReqTRES", "NodeList", "Reason", "JobName",
]

SSHARE_FIELDS = [
    "Account", "User", "RawShares", "NormShares", "RawUsage", "NormUsage",
    "EffectvUsage", "FairShare", "LevelFS",
]

TERMINAL_STATES = {
    "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL",
    "PREEMPTED", "DEADLINE", "BOOT_FAIL", "REVOKED",
}

JOB_NAME_MAX = 128


# --------------------------------------------------------------------------- #
# Small parsing helpers (pure functions; unit tested)
# --------------------------------------------------------------------------- #

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMGTP]?)([nc]?)\s*$", re.IGNORECASE)
_UNIT_TO_MB = {"": 1.0, "K": 1.0 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 ** 2, "P": 1024.0 ** 3}


def parse_size_mb(value, default_unit="M"):
    """Parse a Slurm size like '4000M', '123456K', '2G', '4000' -> MB (float).

    Returns (mb, qualifier) where qualifier is 'n' (per node), 'c' (per cpu)
    or '' for the old ReqMem formats. Returns (None, '') if unparseable/empty.
    """
    if value is None:
        return None, ""
    m = _SIZE_RE.match(str(value))
    if not m:
        return None, ""
    num, unit, qual = m.group(1), m.group(2).upper(), m.group(3).lower()
    if unit == "":
        unit = default_unit
    return float(num) * _UNIT_TO_MB[unit], qual


def parse_req_mem(req_mem, nnodes, ncpus):
    """Job-total requested memory in MB.

    Old Slurm (<21.08): '4000Mn' = per node, '4000Mc' = per CPU.
    New Slurm: plain '4000M' = job total.
    """
    mb, qual = parse_size_mb(req_mem)
    if mb is None:
        return None
    if qual == "n":
        return int(round(mb * max(nnodes or 1, 1)))
    if qual == "c":
        return int(round(mb * max(ncpus or 1, 1)))
    return int(round(mb))


def parse_tres(tres):
    """Parse 'billing=32,cpu=32,mem=128G,node=1,gres/gpu=2,gres/gpu:a100=2'.

    Returns dict with cpus, mem_mb, nodes, gpus, gpu_type, billing (any may be None).
    """
    out = {"cpus": None, "mem_mb": None, "nodes": None, "gpus": None, "gpu_type": None, "billing": None}
    if not tres:
        return out
    typed_gpus = {}
    for item in tres.split(","):
        if "=" not in item:
            continue
        key, _, val = item.partition("=")
        key = key.strip()
        val = val.strip()
        if key == "cpu":
            out["cpus"] = _to_int(val)
        elif key == "mem":
            mb, _ = parse_size_mb(val, default_unit="M")
            out["mem_mb"] = int(round(mb)) if mb is not None else None
        elif key == "node":
            out["nodes"] = _to_int(val)
        elif key == "billing":
            out["billing"] = _to_float(val)
        elif key == "gres/gpu":
            out["gpus"] = _to_int(val)
        elif key.startswith("gres/gpu:"):
            typed_gpus[key[len("gres/gpu:"):]] = _to_int(val) or 0
    if out["gpus"] is None and typed_gpus:
        out["gpus"] = sum(typed_gpus.values())
    if typed_gpus:
        out["gpu_type"] = ",".join(sorted(typed_gpus))
    return out


def parse_duration_seconds(value):
    """Parse '[D-]HH:MM:SS[.mmm]' or 'MM:SS[.mmm]' -> float seconds."""
    if not value:
        return None
    value = value.strip()
    if value in ("INVALID", "UNLIMITED", "Unknown", "None"):
        return None
    days = 0
    if "-" in value:
        d, _, value = value.partition("-")
        days = int(d)
    parts = value.split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), float(parts[1])
        elif len(parts) == 1:
            h, m, s = 0, 0, float(parts[0])
        else:
            return None
    except ValueError:
        return None
    return days * 86400 + h * 3600 + m * 60 + s


def parse_timelimit_seconds(raw):
    """TimelimitRaw is minutes, or UNLIMITED / Partition_Limit."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or not raw[0].isdigit():
        return None
    try:
        return int(raw) * 60
    except ValueError:
        return None


def parse_state(state):
    """'CANCELLED by 12345' -> ('CANCELLED', 12345); 'COMPLETED' -> ('COMPLETED', None)."""
    if not state:
        return "", None
    parts = state.strip().split()
    base = parts[0].upper()
    cancelled_by = None
    if base == "CANCELLED" and len(parts) >= 3 and parts[1] == "by":
        cancelled_by = _to_int(parts[2])
    return base, cancelled_by


def parse_exit_code(value):
    if not value or ":" not in value:
        return None, None
    a, _, b = value.partition(":")
    return _to_int(a), _to_int(b)


def parse_job_id(job_id):
    """Return (array_job_id, array_task_id, het_job_id, het_offset) from JobID."""
    array_job = array_task = het_job = het_off = None
    if job_id:
        m = re.match(r"^(\d+)_(\d+)$", job_id)
        if m:
            array_job, array_task = int(m.group(1)), int(m.group(2))
        m = re.match(r"^(\d+)\+(\d+)$", job_id)
        if m:
            het_job, het_off = int(m.group(1)), int(m.group(2))
    return array_job, array_task, het_job, het_off


def parse_slurm_time(value):
    """'2026-09-14T13:45:01' (local time) -> aware datetime in the process TZ, or None."""
    if not value or value in ("Unknown", "None", "N/A"):
        return None
    try:
        naive = dt.datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return naive.astimezone()  # naive -> local (respects TZ env after tzset)


def iso(d):
    return d.isoformat() if d is not None else None


def parse_iso_arg(value):
    """Parse 'YYYY-MM-DD[THH:MM[:SS]]' (naive, local time) without fromisoformat (py3.6)."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).astimezone()
        except ValueError:
            continue
    raise ValueError("bad timestamp: %s" % value)


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf are not valid JSON
        return None
    return f


def split_fields(line, fields):
    """Split a --parsable2 line; extra '|' end up in the last field."""
    parts = line.rstrip("\n").split("|", len(fields) - 1)
    if len(parts) < len(fields):
        parts += [""] * (len(fields) - len(parts))
    return dict(zip(fields, parts))


# --------------------------------------------------------------------------- #
# sacct -> job rows
# --------------------------------------------------------------------------- #

def build_sacct_command(window_start, window_end, allocations_only=False):
    cmd = [
        "sacct", "--allusers", "--parsable2", "--noheader", "--noconvert",
        "--starttime", window_start.strftime("%Y-%m-%dT%H:%M:%S"),
        "--endtime", window_end.strftime("%Y-%m-%dT%H:%M:%S"),
        "--format", ",".join(SACCT_FIELDS),
    ]
    if allocations_only:
        cmd.append("--allocations")
    return cmd


def parse_sacct_lines(lines, window_start, window_end):
    """Turn sacct lines into finished job dicts whose End is inside the window.

    Steps (JobIDRaw containing '.') are folded into their parent allocation:
    max(MaxRSS) across steps and sum(NTasks).
    """
    jobs = {}          # parent id -> allocation row dict
    step_rss = {}      # parent id -> max rss MB seen on any step
    step_ntasks = {}   # parent id -> sum ntasks
    order = []

    for line in lines:
        if not line.strip():
            continue
        row = split_fields(line, SACCT_FIELDS)
        raw = row["JobIDRaw"]
        parent, dot, _ = raw.partition(".")
        if dot:
            mb, _ = parse_size_mb(row.get("MaxRSS"), default_unit="K")
            if mb is not None:
                step_rss[parent] = max(step_rss.get(parent, 0.0), mb)
            n = _to_int(row.get("NTasks"))
            if n:
                step_ntasks[parent] = step_ntasks.get(parent, 0) + n
            continue
        if parent not in jobs:
            order.append(parent)
        jobs[parent] = row

    out = []
    for parent in order:
        row = jobs[parent]
        end = parse_slurm_time(row["End"])
        if end is None or not (window_start <= end < window_end):
            continue
        state, cancelled_by = parse_state(row["State"])
        if state not in TERMINAL_STATES:
            continue
        job = finalize_job(row, state, cancelled_by, end,
                           step_rss.get(parent), step_ntasks.get(parent))
        out.append(job)
    return out


def finalize_job(row, state, cancelled_by, end, step_max_rss_mb, step_ntasks):
    submit = parse_slurm_time(row["Submit"])
    start = parse_slurm_time(row["Start"])
    elapsed = _to_int(row["ElapsedRaw"]) or 0
    alloc_cpus = _to_int(row["AllocCPUS"]) or 0
    req_cpus = _to_int(row["ReqCPUS"]) or 0
    nnodes = _to_int(row["NNodes"]) or 0
    alloc = parse_tres(row["AllocTRES"])
    req = parse_tres(row["ReqTRES"])
    req_mem_mb = parse_req_mem(row["ReqMem"], nnodes, alloc_cpus or req_cpus)
    alloc_mem_mb = alloc["mem_mb"]
    if alloc_mem_mb is None:
        alloc_mem_mb = req_mem_mb
    # Allocation row MaxRSS is normally empty; steps carry it.
    own_rss, _ = parse_size_mb(row.get("MaxRSS"), default_unit="K")
    max_rss = None
    for candidate in (own_rss, step_max_rss_mb):
        if candidate is not None:
            max_rss = candidate if max_rss is None else max(max_rss, candidate)
    exit_code, signal = parse_exit_code(row["ExitCode"])
    array_job, array_task, het_job, het_off = parse_job_id(row["JobID"])
    cpu_time_raw = _to_int(row["CPUTimeRAW"])
    if cpu_time_raw is None:
        cpu_time_raw = elapsed * alloc_cpus
    wait_s = None
    if submit is not None and start is not None:
        wait_s = max(int((start - submit).total_seconds()), 0)
    gpus = alloc["gpus"]
    if gpus is None and req["gpus"] is not None and alloc_cpus:
        gpus = req["gpus"]
    return {
        "job_id_raw": row["JobIDRaw"],
        "job_id": row["JobID"],
        "array_job_id": array_job,
        "array_task_id": array_task,
        "het_job_id": het_job,
        "het_offset": het_off,
        "user": row["User"],
        "account": row["Account"],
        "partition": row["Partition"],
        "qos": row["QOS"],
        "job_name": row["JobName"][:JOB_NAME_MAX],
        "state": state,
        "cancelled_by": cancelled_by,
        "exit_code": exit_code,
        "signal": signal,
        "submit": iso(submit),
        "start": iso(start),
        "end": iso(end),
        "elapsed_s": elapsed,
        "timelimit_s": parse_timelimit_seconds(row["TimelimitRaw"]),
        "wait_s": wait_s,
        "alloc_cpus": alloc_cpus,
        "req_cpus": req_cpus,
        "nnodes": nnodes,
        "node_list": row["NodeList"],
        "alloc_mem_mb": alloc_mem_mb,
        "req_mem_mb": req_mem_mb,
        "max_rss_mb": int(round(max_rss)) if max_rss is not None else None,
        "mem_eff_approx": nnodes > 1,
        "total_cpu_s": parse_duration_seconds(row["TotalCPU"]),
        "cpu_seconds_alloc": cpu_time_raw,
        "gpus": gpus,
        "gpu_type": alloc["gpu_type"] or req["gpu_type"],
        "billing": alloc["billing"],
        "alloc_tres": row["AllocTRES"],
        "req_tres": row["ReqTRES"],
        "reason": row["Reason"],
        "ntasks": step_ntasks,
    }


# --------------------------------------------------------------------------- #
# scontrol show node / partition
# --------------------------------------------------------------------------- #

_KV_SPLIT_RE = re.compile(r"\s+(?=[A-Za-z][A-Za-z0-9_/:.]*=)")


def parse_oneliner(line):
    """Parse 'Key=Val Key2=Val with spaces Key3=' into a dict."""
    out = {}
    for tok in _KV_SPLIT_RE.split(line.strip()):
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        out[k] = v
    return out


def parse_node_lines(lines):
    nodes = []
    for line in lines:
        if not line.strip() or "NodeName=" not in line:
            continue
        kv = parse_oneliner(line)
        cfg = parse_tres(kv.get("CfgTRES", ""))
        alloc = parse_tres(kv.get("AllocTRES", ""))
        gres = kv.get("Gres", "")
        gpus_total = cfg["gpus"]
        gpu_type = cfg["gpu_type"]
        if gpus_total is None and gres and gres != "(null)":
            # Gres=gpu:a100:4(S:0-1) or gpu:4
            total = 0
            types = []
            for g in gres.split(","):
                g = g.split("(")[0]
                parts = g.split(":")
                if parts[0] != "gpu":
                    continue
                if len(parts) == 3:
                    types.append(parts[1])
                    total += _to_int(parts[2]) or 0
                elif len(parts) == 2:
                    total += _to_int(parts[1]) or 0
            gpus_total = total
            gpu_type = ",".join(sorted(set(types))) or None
        parts = [p for p in kv.get("Partitions", "").split(",") if p]
        nodes.append({
            "node_name": kv.get("NodeName"),
            "state": kv.get("State", ""),
            "cpus_total": _to_int(kv.get("CPUTot")),
            "cpus_alloc": _to_int(kv.get("CPUAlloc")),
            "mem_mb_total": _to_int(kv.get("RealMemory")),
            "mem_mb_alloc": _to_int(kv.get("AllocMem")),
            "gpus_total": gpus_total or 0,
            "gpus_alloc": alloc["gpus"] or 0,
            "gpu_type": gpu_type,
            "partitions": parts,
            "features": kv.get("AvailableFeatures") or kv.get("Features"),
        })
    return nodes


def parse_partition_lines(lines):
    parts = []
    for line in lines:
        if not line.strip() or "PartitionName=" not in line:
            continue
        kv = parse_oneliner(line)
        parts.append({
            "partition": kv.get("PartitionName"),
            "state": kv.get("State", ""),
            "total_cpus": _to_int(kv.get("TotalCPUs")),
            "total_nodes": _to_int(kv.get("TotalNodes")),
            "max_time_s": _slurm_timelimit_to_s(kv.get("MaxTime")),
            "default_time_s": _slurm_timelimit_to_s(kv.get("DefaultTime")),
            "nodes": kv.get("Nodes"),
        })
    return parts


def _slurm_timelimit_to_s(v):
    if not v or v in ("UNLIMITED", "NONE", "INVALID"):
        return None
    s = parse_duration_seconds(v)
    return int(s) if s is not None else None


def parse_sinfo_lines(lines):
    """Fallback: sinfo -N --noheader -o '%N|%P|%T|%c|%C|%m|%G'."""
    nodes = {}
    for line in lines:
        if not line.strip():
            continue
        f = split_fields(line, ["N", "P", "T", "c", "C", "m", "G"])
        name, part, state, cpus, aiot, mem, gres = f["N"], f["P"], f["T"], f["c"], f["C"], f["m"], f["G"]
        aiot_parts = aiot.split("/")
        alloc = _to_int(aiot_parts[0]) if aiot_parts else None
        n = nodes.setdefault(name, {
            "node_name": name, "state": state.upper(), "cpus_total": _to_int(cpus),
            "cpus_alloc": alloc, "mem_mb_total": _to_int(mem), "mem_mb_alloc": None,
            "gpus_total": 0, "gpus_alloc": 0, "gpu_type": None, "partitions": [], "features": None,
        })
        part = part.rstrip("*")
        if part not in n["partitions"]:
            n["partitions"].append(part)
        if gres and gres != "(null)":
            total, types = 0, []
            for g in gres.split(","):
                p = g.split("(")[0].split(":")
                if p[0] == "gpu":
                    if len(p) == 3:
                        types.append(p[1]); total += _to_int(p[2]) or 0
                    elif len(p) == 2:
                        total += _to_int(p[1]) or 0
            n["gpus_total"] = total
            n["gpu_type"] = ",".join(sorted(set(types))) or None
    return list(nodes.values())


# --------------------------------------------------------------------------- #
# sshare
# --------------------------------------------------------------------------- #

def parse_sshare_lines(lines):
    rows = []
    for line in lines:
        if not line.strip():
            continue
        r = split_fields(line, SSHARE_FIELDS)
        raw_account = r["Account"]
        account = raw_account.strip()
        if not account:
            continue
        raw_shares = r["RawShares"].strip()
        rows.append({
            "account": account,
            "depth": len(raw_account) - len(raw_account.lstrip(" ")),
            "user": r["User"].strip() or None,
            "raw_shares": _to_float(raw_shares) if raw_shares.lower() != "parent" else None,
            "shares_parent": raw_shares.lower() == "parent",
            "norm_shares": _to_float(r["NormShares"]),
            "raw_usage": _to_float(r["RawUsage"]),
            "norm_usage": _to_float(r["NormUsage"]),
            "effective_usage": _to_float(r["EffectvUsage"]),
            "fairshare": _to_float(r["FairShare"]),
            "level_fs": _to_float(r["LevelFS"]),
        })
    return rows


# --------------------------------------------------------------------------- #
# Running commands
# --------------------------------------------------------------------------- #

def run_lines(cmd, timeout=3600):
    """Run a command and yield stdout lines (streamed)."""
    log("running: %s" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, bufsize=1)
    try:
        for line in proc.stdout:
            yield line
    finally:
        proc.stdout.close()
    err = proc.stderr.read()
    proc.stderr.close()
    rc = proc.wait(timeout=timeout)
    if rc != 0:
        raise RuntimeError("%s failed (rc=%s): %s" % (cmd[0], rc, err.strip()[:500]))


def read_lines(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield line


def slurm_version():
    try:
        out = subprocess.check_output(["sacct", "--version"], universal_newlines=True, timeout=30)
        return out.strip()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Envelope + transport
# --------------------------------------------------------------------------- #

def make_envelope(cluster, kind, rows, tz_name, window_start=None, window_end=None, taken_at=None, slurm_ver=None):
    return {
        "cluster": cluster,
        "kind": kind,
        "collector_version": COLLECTOR_VERSION,
        "slurm_version": slurm_ver,
        "timezone": tz_name,
        "window_start": iso(window_start),
        "window_end": iso(window_end),
        "taken_at": iso(taken_at or dt.datetime.now().astimezone()),
        "rows": rows,
    }


def envelope_bytes(env):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(json.dumps(env, separators=(",", ":")).encode("utf-8"))
    return buf.getvalue()


def post_envelope(url, token, kind, payload, retries=3, timeout=600):
    endpoint = url.rstrip("/") + "/api/v1/ingest/" + kind
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(endpoint, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "Authorization": "Bearer " + token,
            "User-Agent": "hpcusage-collector/" + COLLECTOR_VERSION,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                log("POST %s -> %s %s" % (endpoint, resp.status, body[:300]))
                return True
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            last = "HTTP %s: %s" % (e.code, body)
            if 400 <= e.code < 500 and e.code != 429:
                log("POST %s failed permanently: %s" % (endpoint, last))
                return False
        except (urllib.error.URLError, OSError) as e:
            last = str(e)
        log("POST %s attempt %d failed: %s" % (endpoint, attempt, last))
        time.sleep(2 ** attempt)
    return False


def spool_write(spool_dir, kind, cluster, payload, tag):
    os.makedirs(spool_dir, exist_ok=True)
    name = "%s-%s-%s-%d.json.gz" % (kind, cluster, tag, int(time.time()))
    path = os.path.join(spool_dir, name)
    with open(path, "wb") as fh:
        fh.write(payload)
    log("spooled %s (%d bytes)" % (path, len(payload)))
    return path


def spool_resend(spool_dir, url, token):
    if not os.path.isdir(spool_dir):
        return
    for name in sorted(os.listdir(spool_dir)):
        if not name.endswith(".json.gz"):
            continue
        kind = name.split("-", 1)[0]
        path = os.path.join(spool_dir, name)
        with open(path, "rb") as fh:
            payload = fh.read()
        log("resending spooled %s" % name)
        if post_envelope(url, token, kind, payload, retries=1):
            os.remove(path)


def deliver(env, kind, args, tag):
    payload = envelope_bytes(env)
    log("%s: %d rows, %d bytes gzipped" % (kind, len(env["rows"]), len(payload)))
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        path = os.path.join(args.output_dir, "%s-%s-%s.json.gz" % (kind, env["cluster"], tag))
        with open(path, "wb") as fh:
            fh.write(payload)
        log("wrote %s" % path)
    if args.dry_run:
        return
    if not args.url or not args.token:
        die("url and token are required to POST (or use --dry-run)")
    if not post_envelope(args.url, args.token, kind, payload):
        if not args.no_spool:
            spool_write(args.spool_dir, kind, env["cluster"], payload, tag)


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #

def local_midnight(d):
    """date -> aware datetime at 00:00 local."""
    return dt.datetime(d.year, d.month, d.day).astimezone()


def day_windows(end_date, days_back, chunk_hours=None):
    """Yield (start, end) covering [end_date - days_back, end_date), one day (or chunk) each."""
    for i in range(days_back, 0, -1):
        day = end_date - dt.timedelta(days=i)
        start = local_midnight(day)
        stop = local_midnight(day + dt.timedelta(days=1))
        if not chunk_hours or chunk_hours >= 24:
            yield start, stop
            continue
        cur = start
        step = dt.timedelta(hours=chunk_hours)
        while cur < stop:
            nxt = min(cur + step, stop)
            yield cur, nxt
            cur = nxt


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def log(msg):
    sys.stderr.write("[%s] %s\n" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def die(msg, code=2):
    log("error: " + msg)
    sys.exit(code)


def load_config(path):
    cfg = {}
    if path and os.path.exists(path):
        cp = configparser.ConfigParser()
        cp.read(path)
        if cp.has_section("hpcusage"):
            cfg = dict(cp.items("hpcusage"))
    return cfg


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="hpcusage Slurm collector")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="INI file (default: %(default)s)")
    p.add_argument("--cluster", help="cluster name (overrides config)")
    p.add_argument("--url", help="backend base URL (overrides config)")
    p.add_argument("--token", help="bearer token (overrides config)")
    p.add_argument("--tz", help="cluster timezone, e.g. America/Los_Angeles (overrides config)")
    kinds = p.add_argument_group("what to collect")
    kinds.add_argument("--jobs", action="store_true", help="sacct job accounting")
    kinds.add_argument("--nodes", action="store_true", help="scontrol node + partition snapshot")
    kinds.add_argument("--fairshare", action="store_true", help="sshare snapshot")
    kinds.add_argument("--all", action="store_true", help="jobs + nodes + fairshare")
    win = p.add_argument_group("job window")
    win.add_argument("--date", help="collect jobs ending before this date (YYYY-MM-DD, exclusive); default today")
    win.add_argument("--days-back", type=int, default=2, help="number of full days before --date (default 2)")
    win.add_argument("--chunk-hours", type=int, help="split each day into N-hour sacct calls")
    win.add_argument("--allocations-only", action="store_true", help="pass -X to sacct (no step rows, no MaxRSS)")
    win.add_argument("--sleep", type=float, default=1.0, help="seconds between sacct calls (default 1)")
    io_ = p.add_argument_group("input/output")
    io_.add_argument("--input", help="read sacct/scontrol/sshare output from FILE instead of running the command "
                                     "(applies to the single kind selected)")
    io_.add_argument("--taken-at", help="override snapshot timestamp (ISO) for --nodes/--fairshare")
    io_.add_argument("--output-dir", help="also write each envelope as a .json.gz here")
    io_.add_argument("--dry-run", action="store_true", help="do not POST")
    io_.add_argument("--spool-dir", default=DEFAULT_SPOOL)
    io_.add_argument("--no-spool", action="store_true", help="do not spool failed uploads")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cfg = load_config(args.config)
    args.cluster = args.cluster or cfg.get("cluster")
    args.url = args.url or cfg.get("url")
    args.token = args.token or cfg.get("token")
    args.tz = args.tz or cfg.get("timezone") or os.environ.get("TZ")
    if args.all:
        args.jobs = args.nodes = args.fairshare = True
    if not (args.jobs or args.nodes or args.fairshare):
        die("nothing selected: use --jobs, --nodes, --fairshare or --all")
    if not args.cluster:
        die("--cluster (or cluster= in config) is required")
    if args.input and sum([args.jobs, args.nodes, args.fairshare]) != 1:
        die("--input applies to exactly one kind")

    if args.tz:
        os.environ["TZ"] = args.tz
        time.tzset()
    tz_name = args.tz or time.strftime("%Z")

    if not args.dry_run and args.url and args.token and not args.no_spool:
        spool_resend(args.spool_dir, args.url, args.token)

    ver = None if args.input else slurm_version()
    taken_at = parse_iso_arg(args.taken_at) if args.taken_at else dt.datetime.now().astimezone()

    if args.jobs:
        end_date = dt.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else dt.date.today()
        for start, stop in day_windows(end_date, args.days_back, args.chunk_hours):
            if args.input:
                lines = read_lines(args.input)
            else:
                lines = run_lines(build_sacct_command(start, stop, args.allocations_only))
            rows = parse_sacct_lines(lines, start, stop)
            env = make_envelope(args.cluster, "jobs", rows, tz_name, start, stop, taken_at, ver)
            deliver(env, "jobs", args, start.strftime("%Y%m%dT%H%M"))
            if not args.input and args.sleep:
                time.sleep(args.sleep)

    if args.nodes:
        if args.input:
            nodes = parse_node_lines(read_lines(args.input))
            parts = []
        else:
            try:
                nodes = parse_node_lines(run_lines(["scontrol", "show", "node", "--oneliner"]))
            except (RuntimeError, OSError) as e:
                log("scontrol failed (%s); falling back to sinfo" % e)
                nodes = parse_sinfo_lines(run_lines(["sinfo", "-N", "--noheader", "-o", "%N|%P|%T|%c|%C|%m|%G"]))
            try:
                parts = parse_partition_lines(run_lines(["scontrol", "show", "partition", "--oneliner"]))
            except (RuntimeError, OSError) as e:
                log("scontrol show partition failed (%s)" % e)
                parts = []
        env = make_envelope(args.cluster, "nodes", nodes, tz_name, taken_at=taken_at, slurm_ver=ver)
        env["partitions"] = parts
        deliver(env, "nodes", args, taken_at.strftime("%Y%m%dT%H%M"))

    if args.fairshare:
        if args.input:
            lines = read_lines(args.input)
        else:
            lines = run_lines(["sshare", "-a", "-P", "--noheader", "--format", ",".join(SSHARE_FIELDS)])
        rows = parse_sshare_lines(lines)
        env = make_envelope(args.cluster, "fairshare", rows, tz_name, taken_at=taken_at, slurm_ver=ver)
        deliver(env, "fairshare", args, taken_at.strftime("%Y%m%dT%H%M"))


if __name__ == "__main__":
    main()
