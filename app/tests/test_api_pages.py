import pytest

from .conftest import envelope_from_fixture, post_envelope


@pytest.fixture()
def seeded(client):
    post_envelope(client, envelope_from_fixture("jobs", "sacct_new.txt"))
    post_envelope(client, envelope_from_fixture("nodes", "scontrol_node.txt"))
    post_envelope(client, envelope_from_fixture("fairshare", "sshare.txt"))
    return client


W = {"from": "2026-09-01", "to": "2026-09-30", "cluster": "hive"}


def test_summary(seeded):
    r = seeded.get("/api/v1/summary", params=W)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["jobs"] == 5
    assert s["users"] == 5
    assert s["gpu_hours"] == pytest.approx(48.0)
    assert s["completed"] == 2 and s["timeout"] == 1 and s["oom"] == 1


def test_timeseries_grouped_and_topn(seeded):
    r = seeded.get("/api/v1/usage/timeseries", params={**W, "metric": "cpu_hours", "group_by": "user", "top_n": 2})
    assert r.status_code == 200, r.text
    ts = r.json()
    assert ts["buckets"] == ["2026-09-14"]
    names = [s["name"] for s in ts["series"]]
    assert names[:2] == ["bob", "alice"] and names[-1] == "other"
    assert ts["series"][0]["values"][0] == pytest.approx(1382400 / 3600)


def test_leaderboard(seeded):
    r = seeded.get("/api/v1/leaderboard", params={**W, "metric": "gpu_hours", "by": "account", "limit": 3})
    assert r.status_code == 200
    lb = r.json()
    assert lb["items"][0]["name"] == "bobgrp"
    assert lb["items"][0]["share"] == pytest.approx(1.0)


def test_outcomes_efficiency_utilization_wait(seeded):
    assert seeded.get("/api/v1/outcomes", params=W).json()["totals"]["Timeout"] == 1
    eff = seeded.get("/api/v1/efficiency", params={**W, "min_cpu_hours": 0}).json()
    assert {i["name"] for i in eff["items"]} == {"alice", "bob", "carol", "frank", "grace"}
    util = seeded.get("/api/v1/utilization", params={**W, "resource": "gpu"}).json()
    gpu = next(s for s in util["series"] if s["name"] == "gpu-a100")
    assert gpu["avg_pct"] == pytest.approx(100.0 * 48 / (4 * 48))  # 48 GPU-h used of 4 GPUs x 2 days
    wt = seeded.get("/api/v1/wait_times", params=W).json()
    assert wt["histogram"]["buckets"][0] == "< 1 min"


def test_jobs_and_nodes_and_fairshare(seeded):
    jobs = seeded.get("/api/v1/jobs", params={**W, "gpus_only": "true"}).json()
    assert jobs["total"] == 1 and jobs["items"][0]["user_name"] == "bob"
    nodes = seeded.get("/api/v1/nodes/latest", params={"cluster": "hive"}).json()
    assert nodes["totals"]["nodes"] == 3 and nodes["totals"]["gpus_total"] == 4
    fs = seeded.get("/api/v1/fairshare", params={"cluster": "hive"}).json()
    assert len(fs["rows"]) == 5
    series = seeded.get("/api/v1/fairshare", params={**W, "account": "alicegrp", "user": "alice"}).json()
    assert len(series["t"]) == 1


def test_unknown_cluster_404(seeded):
    assert seeded.get("/api/v1/summary", params={**W, "cluster": "nope"}).status_code == 404


@pytest.mark.parametrize("path", [
    "/", "/c/hive", "/c/hive/leaderboard", "/c/hive/users/alice", "/c/hive/accounts/bobgrp",
    "/c/hive/partitions/high", "/c/hive/utilization", "/c/hive/efficiency", "/c/hive/fairshare",
    "/c/hive/jobs", "/c/hive/nodes", "/admin/ingest",
])
def test_pages_render(seeded, path):
    r = seeded.get(path, params={"from": "2026-09-01", "to": "2026-09-30"})
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
    assert "<html" in r.text.lower()


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"
