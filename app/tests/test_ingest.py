from datetime import date

from sqlalchemy import text

from .conftest import TOKENS, envelope_from_fixture, gz, post_envelope


def test_rejects_bad_token(client):
    env = envelope_from_fixture("jobs", "sacct_new.txt")
    r = post_envelope(client, env, token="nope")
    assert r.status_code == 403
    r = client.post("/api/v1/ingest/jobs", content=b"{}")
    assert r.status_code == 401


def test_token_cluster_must_match_envelope(client):
    env = envelope_from_fixture("jobs", "sacct_new.txt", cluster="farm")
    r = post_envelope(client, env, token="test-token-hive")
    assert r.status_code == 403
    assert "farm" in r.json()["detail"]


def test_kind_must_match_url(client):
    env = envelope_from_fixture("jobs", "sacct_new.txt")
    env["kind"] = "nodes"
    r = client.post("/api/v1/ingest/jobs", content=gz(env),
                    headers={"Authorization": f"Bearer {TOKENS['hive']}", "Content-Encoding": "gzip"})
    assert r.status_code == 400


def test_jobs_ingest_upsert_and_rollup(client, clean_db):
    env = envelope_from_fixture("jobs", "sacct_new.txt")
    r = post_envelope(client, env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 5 and body["updated"] == 0
    assert body["rollup_days"] == ["2026-09-14"]

    with clean_db.connect() as conn:
        jobs = conn.execute(text("SELECT job_id_raw, state, end_day, max_rss_mb, gpus FROM jobs "
                                 "ORDER BY job_id_raw")).all()
        assert [j.job_id_raw for j in jobs] == ["1001", "1002", "1003_7", "1006+1", "1007"]
        assert all(j.end_day == date(2026, 9, 14) for j in jobs)
        assert dict((j.job_id_raw, j.max_rss_mb) for j in jobs)["1001"] == 8192

        du = conn.execute(text("SELECT user_name, partition, job_count, cpu_seconds_alloc, gpu_seconds, jobs_timeout "
                               "FROM daily_usage ORDER BY user_name")).mappings().all()
        assert [d["user_name"] for d in du] == ["alice", "bob", "carol", "frank", "grace"]
        bob = next(d for d in du if d["user_name"] == "bob")
        assert bob["partition"] == "gpu-a100"
        assert bob["gpu_seconds"] == 2 * 86400
        assert bob["jobs_timeout"] == 1
        alice = next(d for d in du if d["user_name"] == "alice")
        assert alice["cpu_seconds_alloc"] == 57600

        # 1002 ran 2026-09-13 23:00 -> 09-14 23:00, so utilization is split across two days
        util = conn.execute(text("SELECT day, partition, cpu_seconds, gpu_seconds FROM daily_partition_util "
                                 "WHERE partition = 'gpu-a100' ORDER BY day")).mappings().all()
        assert [u["day"] for u in util] == [date(2026, 9, 13), date(2026, 9, 14)]
        assert util[0]["gpu_seconds"] == 2 * 3600          # one hour on the 13th
        assert util[1]["gpu_seconds"] == 2 * 23 * 3600     # 23 hours on the 14th
        assert util[1]["cpu_seconds"] == 16 * 23 * 3600

        batch = conn.execute(text("SELECT status, rows_received, rows_inserted FROM ingest_batches")).one()
        assert batch.status == "ok" and batch.rows_received == 5 and batch.rows_inserted == 5

    # Re-posting is idempotent: everything updates, nothing duplicates.
    r = post_envelope(client, env)
    assert r.status_code == 200
    assert r.json()["inserted"] == 0 and r.json()["updated"] == 5
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar() == 5
        assert conn.execute(text("SELECT count(*) FROM daily_usage")).scalar() == 5


def test_nodes_ingest_gives_capacity(client, clean_db):
    post_envelope(client, envelope_from_fixture("jobs", "sacct_new.txt"))
    r = post_envelope(client, envelope_from_fixture("nodes", "scontrol_node.txt"))
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 3 and r.json()["partitions_inserted"] == 2
    with clean_db.connect() as conn:
        row = conn.execute(text("SELECT capacity_cpu_seconds, capacity_gpu_seconds FROM daily_partition_util "
                                "WHERE day = '2026-09-14' AND partition = 'gpu-a100'")).one()
        assert row.capacity_cpu_seconds == 64 * 86400
        assert row.capacity_gpu_seconds == 4 * 86400
        # c-9-9 is DOWN+DRAIN so 'low' capacity counts only c-1-1
        low = conn.execute(text("SELECT capacity_cpu_seconds, cpu_seconds FROM daily_partition_util "
                                "WHERE day = '2026-09-14' AND partition = 'low'")).one()
        assert low.capacity_cpu_seconds == 64 * 86400
        assert low.cpu_seconds == 60  # carol's 1-cpu 60 s job


def test_fairshare_ingest(client, clean_db):
    r = post_envelope(client, envelope_from_fixture("fairshare", "sshare.txt"))
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 5
    with clean_db.connect() as conn:
        rows = conn.execute(text("SELECT account, user_name, shares_parent, level_fs FROM fairshare_snapshots "
                                 "ORDER BY id")).all()
        assert rows[2].account == "alicegrp" and rows[2].user_name == "alice" and rows[2].shares_parent is True


def test_retention_purges_old_jobs(client, clean_db, settings_env, monkeypatch):
    # Fixture jobs ended 2026-09-14; with a 1-day retention they are purged right after rollup,
    # but the rollups survive.
    monkeypatch.setattr(settings_env, "job_retention_days", 1)
    r = post_envelope(client, envelope_from_fixture("jobs", "sacct_new.txt"))
    assert r.status_code == 200
    assert r.json()["purged_jobs"] == 5
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM daily_usage")).scalar() == 5


def test_error_is_recorded_on_batch(client, clean_db):
    env = envelope_from_fixture("jobs", "sacct_new.txt")
    env["rows"][0]["end"] = "not a date"
    r = post_envelope(client, env)
    assert r.status_code == 422
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar() == 0
