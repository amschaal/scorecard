"""Collector parser tests. Standard-library unittest so they run on login nodes:

    python3 -m unittest discover -s collector/tests -v
"""
import datetime as dt
import gzip
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import slurm_collector as sc  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name)) as fh:
        return fh.readlines()


def setUpModule():
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()


class ParseHelpers(unittest.TestCase):
    def test_size(self):
        self.assertEqual(sc.parse_size_mb("4000M"), (4000.0, ""))
        self.assertEqual(sc.parse_size_mb("2G"), (2048.0, ""))
        self.assertEqual(sc.parse_size_mb("1048576K"), (1024.0, ""))
        self.assertEqual(sc.parse_size_mb("4000Mn"), (4000.0, "n"))
        self.assertEqual(sc.parse_size_mb("4000Mc"), (4000.0, "c"))
        self.assertEqual(sc.parse_size_mb("4000", default_unit="K"), (4000.0 / 1024, ""))
        self.assertEqual(sc.parse_size_mb(""), (None, ""))
        self.assertEqual(sc.parse_size_mb(None), (None, ""))

    def test_req_mem(self):
        self.assertEqual(sc.parse_req_mem("4000M", 2, 8), 4000)
        self.assertEqual(sc.parse_req_mem("4000Mn", 2, 8), 8000)
        self.assertEqual(sc.parse_req_mem("4000Mc", 2, 8), 32000)
        self.assertEqual(sc.parse_req_mem("2Gn", 3, 1), 6144)
        self.assertIsNone(sc.parse_req_mem("", 1, 1))

    def test_tres(self):
        t = sc.parse_tres("billing=64,cpu=16,mem=131072M,node=1,gres/gpu=2,gres/gpu:a100=2")
        self.assertEqual(t["cpus"], 16)
        self.assertEqual(t["mem_mb"], 131072)
        self.assertEqual(t["gpus"], 2)
        self.assertEqual(t["gpu_type"], "a100")
        self.assertEqual(t["billing"], 64.0)
        t = sc.parse_tres("cpu=4,mem=8G,node=1,gres/gpu:v100=1,gres/gpu:a100=3")
        self.assertEqual(t["gpus"], 4)
        self.assertEqual(t["gpu_type"], "a100,v100")
        self.assertEqual(t["mem_mb"], 8192)
        self.assertEqual(sc.parse_tres("")["cpus"], None)

    def test_duration(self):
        self.assertEqual(sc.parse_duration_seconds("1-02:03:04.567"), 86400 + 7384.567)
        self.assertEqual(sc.parse_duration_seconds("15:30:00"), 55800)
        self.assertEqual(sc.parse_duration_seconds("00:30.123"), 30.123)
        self.assertIsNone(sc.parse_duration_seconds(""))
        self.assertIsNone(sc.parse_duration_seconds("UNLIMITED"))

    def test_timelimit(self):
        self.assertEqual(sc.parse_timelimit_seconds("1440"), 86400)
        self.assertIsNone(sc.parse_timelimit_seconds("UNLIMITED"))
        self.assertIsNone(sc.parse_timelimit_seconds("Partition_Limit"))

    def test_state(self):
        self.assertEqual(sc.parse_state("CANCELLED by 5001"), ("CANCELLED", 5001))
        self.assertEqual(sc.parse_state("COMPLETED"), ("COMPLETED", None))
        self.assertEqual(sc.parse_state("OUT_OF_MEMORY"), ("OUT_OF_MEMORY", None))

    def test_job_id(self):
        self.assertEqual(sc.parse_job_id("1003_7"), (1003, 7, None, None))
        self.assertEqual(sc.parse_job_id("1006+1"), (None, None, 1006, 1))
        self.assertEqual(sc.parse_job_id("1001"), (None, None, None, None))

    def test_split_fields_pipe_in_last(self):
        d = sc.split_fields("a|b|c|d|e", ["x", "y", "z"])
        self.assertEqual(d, {"x": "a", "y": "b", "z": "c|d|e"})
        d = sc.split_fields("a|b", ["x", "y", "z"])
        self.assertEqual(d["z"], "")

    def test_oneliner(self):
        kv = sc.parse_oneliner("NodeName=c-9-9 State=DOWN+DRAIN Reason=bad dimm [root@2026-09-10T12:00:00] CfgTRES=cpu=64,mem=1M AllocTRES=")
        self.assertEqual(kv["Reason"], "bad dimm [root@2026-09-10T12:00:00]")
        self.assertEqual(kv["CfgTRES"], "cpu=64,mem=1M")
        self.assertEqual(kv["AllocTRES"], "")


class SacctParsing(unittest.TestCase):
    def window(self):
        return sc.local_midnight(dt.date(2026, 9, 14)), sc.local_midnight(dt.date(2026, 9, 15))

    def test_new_format(self):
        start, end = self.window()
        jobs = {j["job_id_raw"]: j for j in sc.parse_sacct_lines(fixture("sacct_new.txt"), start, end)}
        # 1004 running, 1005 ended outside window -> excluded
        self.assertEqual(sorted(jobs), ["1001", "1002", "1003_7", "1006+1", "1007"])

        j = jobs["1001"]
        self.assertEqual(j["user"], "alice")
        self.assertEqual(j["state"], "COMPLETED")
        self.assertEqual(j["alloc_cpus"], 8)
        self.assertEqual(j["alloc_mem_mb"], 32000)
        self.assertEqual(j["req_mem_mb"], 32000)
        self.assertEqual(j["max_rss_mb"], 8192)  # max over steps (8388608K)
        self.assertEqual(j["ntasks"], 10)          # 1 + 1 + 8
        self.assertEqual(j["total_cpu_s"], 55800.0)
        self.assertEqual(j["cpu_seconds_alloc"], 57600)
        self.assertEqual(j["wait_s"], 300)
        self.assertEqual(j["timelimit_s"], 86400)
        self.assertEqual(j["job_name"], "align reads")
        self.assertEqual(j["end"], "2026-09-14T10:05:00-07:00")
        self.assertFalse(j["mem_eff_approx"])
        self.assertIsNone(j["gpus"])

        j = jobs["1002"]
        self.assertEqual(j["state"], "TIMEOUT")
        self.assertEqual(j["gpus"], 2)
        self.assertEqual(j["gpu_type"], "a100")
        self.assertEqual(j["alloc_mem_mb"], 131072)
        self.assertEqual(j["req_mem_mb"], 131072)  # 128G
        self.assertEqual(j["billing"], 64.0)
        self.assertEqual(j["job_name"], "train|model|v2")  # pipes preserved
        self.assertEqual(j["max_rss_mb"], 61440)
        self.assertAlmostEqual(j["total_cpu_s"], 86400 + 7384.567)

        j = jobs["1003_7"]
        self.assertEqual(j["state"], "CANCELLED")
        self.assertEqual(j["cancelled_by"], 5001)
        self.assertEqual((j["array_job_id"], j["array_task_id"]), (1003, 7))

        j = jobs["1006+1"]
        self.assertEqual((j["het_job_id"], j["het_offset"]), (1006, 1))
        self.assertIsNone(j["timelimit_s"])

        j = jobs["1007"]
        self.assertEqual(j["state"], "OUT_OF_MEMORY")
        self.assertEqual((j["exit_code"], j["signal"]), (0, 125))

    def test_old_format(self):
        start, end = self.window()
        jobs = {j["job_id_raw"]: j for j in sc.parse_sacct_lines(fixture("sacct_old.txt"), start, end)}
        self.assertEqual(jobs["2001"]["req_mem_mb"], 32000)      # 4000Mc * 8 cpus
        self.assertEqual(jobs["2001"]["alloc_mem_mb"], 64000)    # from AllocTRES
        self.assertTrue(jobs["2001"]["mem_eff_approx"])
        self.assertEqual(jobs["2002"]["req_mem_mb"], 30000)      # 10000Mn * 3 nodes
        self.assertEqual(jobs["2002"]["alloc_mem_mb"], 30000)    # no AllocTRES -> falls back to ReqMem
        self.assertIsNone(jobs["2002"]["max_rss_mb"])

    def test_window_excludes_previous_day(self):
        start = sc.local_midnight(dt.date(2026, 9, 13))
        end = sc.local_midnight(dt.date(2026, 9, 14))
        jobs = [j["job_id_raw"] for j in sc.parse_sacct_lines(fixture("sacct_new.txt"), start, end)]
        self.assertEqual(jobs, ["1005"])

    def test_command(self):
        start, end = self.window()
        cmd = sc.build_sacct_command(start, end, allocations_only=True)
        self.assertEqual(cmd[0], "sacct")
        self.assertIn("--noconvert", cmd)
        self.assertIn("2026-09-14T00:00:00", cmd)
        self.assertIn("2026-09-15T00:00:00", cmd)
        self.assertEqual(cmd[-1], "--allocations")
        self.assertTrue(cmd[cmd.index("--format") + 1].endswith(",JobName"))


class SnapshotParsing(unittest.TestCase):
    def test_nodes(self):
        nodes = {n["node_name"]: n for n in sc.parse_node_lines(fixture("scontrol_node.txt"))}
        self.assertEqual(sorted(nodes), ["c-1-1", "c-9-9", "gpu-1"])
        n = nodes["c-1-1"]
        self.assertEqual((n["cpus_total"], n["cpus_alloc"]), (64, 48))
        self.assertEqual((n["mem_mb_total"], n["mem_mb_alloc"]), (515000, 256000))
        self.assertEqual(n["partitions"], ["low", "med", "high"])
        self.assertEqual(n["gpus_total"], 0)
        g = nodes["gpu-1"]
        self.assertEqual((g["gpus_total"], g["gpus_alloc"], g["gpu_type"]), (4, 2, "a100"))
        d = nodes["c-9-9"]
        self.assertEqual(d["state"], "DOWN+DRAIN")
        self.assertEqual(d["gpus_alloc"], 0)

    def test_partitions(self):
        parts = {p["partition"]: p for p in sc.parse_partition_lines(fixture("scontrol_partition.txt"))}
        self.assertEqual(parts["high"]["max_time_s"], 30 * 86400)
        self.assertEqual(parts["high"]["default_time_s"], 3600)
        self.assertEqual(parts["high"]["total_cpus"], 128)
        self.assertIsNone(parts["gpu-a100"]["max_time_s"])
        self.assertIsNone(parts["gpu-a100"]["default_time_s"])

    def test_sinfo_fallback(self):
        nodes = {n["node_name"]: n for n in sc.parse_sinfo_lines(fixture("sinfo.txt"))}
        self.assertEqual(nodes["c-1-1"]["partitions"], ["low", "high"])
        self.assertEqual(nodes["c-1-1"]["cpus_alloc"], 48)
        self.assertEqual(nodes["gpu-1"]["gpus_total"], 4)
        self.assertEqual(nodes["gpu-1"]["gpu_type"], "a100")

    def test_sshare(self):
        rows = sc.parse_sshare_lines(fixture("sshare.txt"))
        self.assertEqual(len(rows), 5)
        root = rows[0]
        self.assertEqual(root["account"], "root")
        self.assertIsNone(root["user"])
        alice = rows[2]
        self.assertEqual((alice["account"], alice["user"], alice["depth"]), ("alicegrp", "alice", 2))
        self.assertTrue(alice["shares_parent"])
        self.assertIsNone(alice["raw_shares"])
        self.assertIsNone(alice["level_fs"])  # inf -> None (not valid JSON)
        bob = rows[4]
        self.assertIsNone(bob["level_fs"])    # nan -> None
        self.assertEqual(bob["raw_shares"], 1.0)


class Windows(unittest.TestCase):
    def test_day_windows(self):
        w = list(sc.day_windows(dt.date(2026, 9, 15), 2))
        self.assertEqual(len(w), 2)
        self.assertEqual(w[0][0].date(), dt.date(2026, 9, 13))
        self.assertEqual(w[1][1].date(), dt.date(2026, 9, 15))
        self.assertEqual(len(list(sc.day_windows(dt.date(2026, 9, 15), 1, chunk_hours=6))), 4)


class EndToEnd(unittest.TestCase):
    def test_dry_run_writes_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc.main(["--cluster", "hive", "--jobs", "--input", os.path.join(FIX, "sacct_new.txt"),
                     "--date", "2026-09-15", "--days-back", "1", "--dry-run", "--output-dir", tmp,
                     "--tz", "America/Los_Angeles", "--config", "/nonexistent"])
            files = os.listdir(tmp)
            self.assertEqual(len(files), 1)
            with gzip.open(os.path.join(tmp, files[0]), "rt") as fh:
                env = json.load(fh)
            self.assertEqual(env["cluster"], "hive")
            self.assertEqual(env["kind"], "jobs")
            self.assertEqual(env["timezone"], "America/Los_Angeles")
            self.assertEqual(env["window_start"], "2026-09-14T00:00:00-07:00")
            self.assertEqual(len(env["rows"]), 5)

    def test_nodes_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc.main(["--cluster", "hive", "--nodes", "--input", os.path.join(FIX, "scontrol_node.txt"),
                     "--dry-run", "--output-dir", tmp, "--taken-at", "2026-09-14T02:15",
                     "--tz", "America/Los_Angeles", "--config", "/nonexistent"])
            files = os.listdir(tmp)
            with gzip.open(os.path.join(tmp, files[0]), "rt") as fh:
                env = json.load(fh)
            self.assertEqual(env["kind"], "nodes")
            self.assertEqual(env["taken_at"], "2026-09-14T02:15:00-07:00")
            self.assertEqual(len(env["rows"]), 3)


class Spool(unittest.TestCase):
    def _spool(self, tmp, names):
        for n in names:
            with open(os.path.join(tmp, n), "wb") as fh:
                fh.write(b"payload-" + n.encode())

    def test_resend_deletes_only_successes(self):
        calls = []

        def fake_post(url, token, kind, payload, retries=3, timeout=600):
            calls.append((kind, payload, retries))
            return kind != "nodes"

        orig = sc.post_envelope
        sc.post_envelope = fake_post
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self._spool(tmp, ["jobs-hive-20260914T0000-1.json.gz", "nodes-hive-20260915T0215-2.json.gz",
                                  "notes.txt"])
                self.assertEqual(sc.spool_resend(tmp, "http://x", "t"), (1, 1))
                self.assertEqual(sorted(os.listdir(tmp)), ["nodes-hive-20260915T0215-2.json.gz", "notes.txt"])
        finally:
            sc.post_envelope = orig
        self.assertEqual([(c[0], c[2]) for c in calls], [("jobs", 1), ("nodes", 1)])
        self.assertEqual(calls[0][1], b"payload-jobs-hive-20260914T0000-1.json.gz")

    def test_resend_missing_dir(self):
        self.assertEqual(sc.spool_resend("/nonexistent/spool", "http://x", "t"), (0, 0))

    def test_resend_flag_collects_nothing(self):
        posted, ran = [], []
        orig_post, orig_run = sc.post_envelope, sc.run_lines
        sc.post_envelope = lambda url, token, kind, payload, retries=3, timeout=600: posted.append(kind) or True
        sc.run_lines = lambda cmd: ran.append(cmd) or []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self._spool(tmp, ["fairshare-hive-20260915T0215-3.json.gz"])
                with self.assertRaises(SystemExit) as cm:
                    sc.main(["--cluster", "hive", "--url", "http://x", "--token", "t", "--resend",
                             "--spool-dir", tmp])
                self.assertEqual(cm.exception.code, 0)
                self.assertEqual(os.listdir(tmp), [])
        finally:
            sc.post_envelope, sc.run_lines = orig_post, orig_run
        self.assertEqual(posted, ["fairshare"])
        self.assertEqual(ran, [])

    def test_resend_flag_rejects_kinds(self):
        with self.assertRaises(SystemExit) as cm:
            sc.main(["--cluster", "hive", "--url", "http://x", "--token", "t", "--resend", "--jobs"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
