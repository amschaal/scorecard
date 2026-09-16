"""Recompute materialized daily rollups for a set of days (DELETE + INSERT ... SELECT).

Two rollups:
  daily_usage          — jobs attributed to the day they ENDED (accounting view).
  daily_partition_util — allocated resource-seconds split across the calendar days
                         a job actually ran, compared with the capacity seen in the
                         node snapshot for that day (utilization view).
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

DAILY_USAGE_SQL = text("""
INSERT INTO daily_usage (
    cluster_id, day, user_name, account, partition, qos,
    job_count, jobs_completed, jobs_failed, jobs_timeout, jobs_cancelled, jobs_oom,
    jobs_node_fail, jobs_preempted, jobs_under_5min,
    cpu_seconds_alloc, cpu_seconds_used, gpu_seconds, mem_mb_seconds, node_seconds,
    wall_seconds, billing_seconds, wait_seconds_sum, jobs_with_wait,
    elapsed_limited_sum, timelimit_s_sum, max_rss_mb_sum, alloc_mem_mb_sum, jobs_with_rss
)
SELECT
    cluster_id, end_day, user_name, account, partition, qos,
    count(*),
    count(*) FILTER (WHERE state = 'COMPLETED'),
    count(*) FILTER (WHERE state = 'FAILED'),
    count(*) FILTER (WHERE state = 'TIMEOUT'),
    count(*) FILTER (WHERE state = 'CANCELLED'),
    count(*) FILTER (WHERE state = 'OUT_OF_MEMORY'),
    count(*) FILTER (WHERE state = 'NODE_FAIL'),
    count(*) FILTER (WHERE state = 'PREEMPTED'),
    count(*) FILTER (WHERE elapsed_s < 300),
    coalesce(sum(cpu_seconds_alloc), 0),
    coalesce(sum(total_cpu_s), 0),
    coalesce(sum(coalesce(gpus, 0)::bigint * elapsed_s), 0),
    coalesce(sum(coalesce(alloc_mem_mb, 0)::bigint * elapsed_s), 0),
    coalesce(sum(nnodes::bigint * elapsed_s), 0),
    coalesce(sum(elapsed_s::bigint), 0),
    coalesce(sum(coalesce(billing, 0) * elapsed_s), 0),
    coalesce(sum(wait_s::bigint) FILTER (WHERE wait_s IS NOT NULL), 0),
    count(*) FILTER (WHERE wait_s IS NOT NULL),
    coalesce(sum(elapsed_s::bigint) FILTER (WHERE timelimit_s IS NOT NULL), 0),
    coalesce(sum(timelimit_s::bigint) FILTER (WHERE timelimit_s IS NOT NULL), 0),
    coalesce(sum(max_rss_mb) FILTER (WHERE max_rss_mb IS NOT NULL AND alloc_mem_mb > 0), 0),
    coalesce(sum(alloc_mem_mb) FILTER (WHERE max_rss_mb IS NOT NULL AND alloc_mem_mb > 0), 0),
    count(*) FILTER (WHERE max_rss_mb IS NOT NULL AND alloc_mem_mb > 0)
FROM jobs
WHERE cluster_id = :cid AND end_day = ANY(:days)
GROUP BY cluster_id, end_day, user_name, account, partition, qos
""")

DAILY_PARTITION_UTIL_SQL = text("""
WITH days AS (
    SELECT unnest(CAST(:days AS date[])) AS day
), bounds AS (
    SELECT day,
           (day::timestamp AT TIME ZONE :tz)       AS d0,
           ((day + 1)::timestamp AT TIME ZONE :tz) AS d1
    FROM days
), split AS (
    SELECT b.day, j.partition,
           EXTRACT(EPOCH FROM (LEAST(j.end_time, b.d1) - GREATEST(j.start_time, b.d0))) AS secs,
           j.alloc_cpus, coalesce(j.gpus, 0) AS gpus, coalesce(j.alloc_mem_mb, 0) AS mem_mb, j.nnodes,
           (j.start_time >= b.d0 AND j.start_time < b.d1) AS started_today,
           j.wait_s
    FROM bounds b
    JOIN jobs j
      ON j.cluster_id = :cid
     AND j.start_time IS NOT NULL
     AND j.elapsed_s > 0
     AND j.start_time < b.d1
     AND j.end_time > b.d0
), usage AS (
    SELECT day, partition,
           sum(secs * alloc_cpus) AS cpu_seconds,
           sum(secs * gpus)       AS gpu_seconds,
           sum(secs * mem_mb)     AS mem_mb_seconds,
           sum(secs * nnodes)     AS node_seconds,
           count(*) FILTER (WHERE started_today) AS jobs_started,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_s)
               FILTER (WHERE started_today AND wait_s IS NOT NULL) AS wait_p50,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY wait_s)
               FILTER (WHERE started_today AND wait_s IS NOT NULL) AS wait_p90,
           avg(wait_s) FILTER (WHERE started_today AND wait_s IS NOT NULL) AS wait_mean
    FROM split
    GROUP BY day, partition
), snap AS (
    -- Latest node snapshot taken up to a day after the day ends (the 02:15 next-morning
    -- snapshot represents "yesterday"); fall back to the earliest snapshot for backfills.
    SELECT b.day,
           coalesce(
             (SELECT max(n.taken_at) FROM node_snapshots n
               WHERE n.cluster_id = :cid AND n.taken_at < b.d1 + interval '1 day'),
             (SELECT min(n.taken_at) FROM node_snapshots n WHERE n.cluster_id = :cid)
           ) AS taken_at
    FROM bounds b
), cap AS (
    SELECT s.day, p.partition,
           sum(n.cpus_total)   * 86400.0 AS cap_cpu,
           sum(n.gpus_total)   * 86400.0 AS cap_gpu,
           sum(n.mem_mb_total) * 86400.0 AS cap_mem,
           count(*)            * 86400.0 AS cap_node
    FROM snap s
    JOIN node_snapshots n ON n.cluster_id = :cid AND n.taken_at = s.taken_at
    CROSS JOIN LATERAL unnest(n.partitions) AS p(partition)
    WHERE n.state NOT LIKE 'DOWN%' AND n.state NOT LIKE '%DRAIN%' AND n.state NOT LIKE 'FUTURE%'
    GROUP BY s.day, p.partition
)
INSERT INTO daily_partition_util (
    cluster_id, day, partition, cpu_seconds, gpu_seconds, mem_mb_seconds, node_seconds,
    capacity_cpu_seconds, capacity_gpu_seconds, capacity_mem_mb_seconds, capacity_node_seconds,
    jobs_started, wait_p50_s, wait_p90_s, wait_mean_s
)
SELECT :cid,
       coalesce(u.day, c.day),
       coalesce(u.partition, c.partition),
       coalesce(u.cpu_seconds, 0), coalesce(u.gpu_seconds, 0),
       coalesce(u.mem_mb_seconds, 0), coalesce(u.node_seconds, 0),
       c.cap_cpu, c.cap_gpu, c.cap_mem, c.cap_node,
       coalesce(u.jobs_started, 0), u.wait_p50, u.wait_p90, u.wait_mean
FROM usage u
FULL OUTER JOIN cap c ON c.day = u.day AND c.partition = u.partition
""")


def recompute_daily_usage(db: Session, cluster_id: int, days: set[date]) -> int:
    if not days:
        return 0
    day_list = sorted(days)
    db.execute(text("DELETE FROM daily_usage WHERE cluster_id = :cid AND day = ANY(:days)"),
               {"cid": cluster_id, "days": day_list})
    res = db.execute(DAILY_USAGE_SQL, {"cid": cluster_id, "days": day_list})
    return res.rowcount


def recompute_partition_util(db: Session, cluster_id: int, tz: str, days: set[date]) -> int:
    if not days:
        return 0
    day_list = sorted(days)
    db.execute(text("DELETE FROM daily_partition_util WHERE cluster_id = :cid AND day = ANY(:days)"),
               {"cid": cluster_id, "days": day_list})
    res = db.execute(DAILY_PARTITION_UTIL_SQL, {"cid": cluster_id, "tz": tz, "days": day_list})
    return res.rowcount
