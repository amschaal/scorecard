"""Pydantic models for collector envelopes. Unknown keys are ignored so older/newer
collectors interoperate."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore")


class JobRow(_Row):
    job_id_raw: str
    job_id: str
    array_job_id: int | None = None
    array_task_id: int | None = None
    het_job_id: int | None = None
    het_offset: int | None = None
    user: str = ""
    account: str = ""
    partition: str = ""
    qos: str = ""
    job_name: str | None = None
    state: str
    cancelled_by: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    submit: datetime | None = None
    start: datetime | None = None
    end: datetime
    elapsed_s: int = 0
    timelimit_s: int | None = None
    wait_s: int | None = None
    alloc_cpus: int = 0
    req_cpus: int = 0
    nnodes: int = 0
    node_list: str | None = None
    alloc_mem_mb: int | None = None
    req_mem_mb: int | None = None
    max_rss_mb: int | None = None
    mem_eff_approx: bool = False
    total_cpu_s: float | None = None
    cpu_seconds_alloc: int = 0
    gpus: int | None = None
    gpu_type: str | None = None
    billing: float | None = None
    ntasks: int | None = None
    alloc_tres: str | None = None
    req_tres: str | None = None
    reason: str | None = None


class NodeRow(_Row):
    node_name: str
    state: str = ""
    cpus_total: int | None = 0
    cpus_alloc: int | None = 0
    mem_mb_total: int | None = 0
    mem_mb_alloc: int | None = 0
    gpus_total: int | None = 0
    gpus_alloc: int | None = 0
    gpu_type: str | None = None
    partitions: list[str] = Field(default_factory=list)
    features: str | None = None


class PartitionRow(_Row):
    partition: str
    state: str = ""
    total_cpus: int | None = None
    total_nodes: int | None = None
    max_time_s: int | None = None
    default_time_s: int | None = None
    nodes: str | None = None


class FairshareRow(_Row):
    account: str
    user: str | None = None
    depth: int = 0
    raw_shares: float | None = None
    shares_parent: bool = False
    norm_shares: float | None = None
    raw_usage: float | None = None
    norm_usage: float | None = None
    effective_usage: float | None = None
    fairshare: float | None = None
    level_fs: float | None = None


class Envelope(_Row):
    cluster: str
    kind: Literal["jobs", "nodes", "fairshare"]
    collector_version: str | None = None
    slurm_version: str | None = None
    timezone: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    taken_at: datetime | None = None
    rows: list[dict]
    partitions: list[dict] | None = None  # only on kind=nodes
