import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_ogm_sync_capacity.py"
spec = importlib.util.spec_from_file_location("capacity", SCRIPT)
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


class CapacityTests(TestCase):
    def check(self, free=20, node_free=20, blocks=None, failure=None):
        nodes = {
            "nodes": {
                "one": {
                    "name": "production",
                    "fs": {"total": {"total_in_bytes": 100, "available_in_bytes": node_free}},
                }
            }
        }
        settings = {"physical-index": {"settings": {"index": {"blocks": blocks or {}}}}}
        with (
            patch.object(
                capacity.shutil, "disk_usage", return_value=SimpleNamespace(total=100, free=free)
            ),
            patch.object(capacity, "get_json", side_effect=failure or [nodes, settings]),
        ):
            return capacity.check_capacity("/", "http://localhost:9200", "search-alias")

    def test_healthy_and_unblocked(self):
        self.assertTrue(self.check(blocks={"write": "false"})["ready"])

    def test_host_disk_reserve(self):
        report = self.check(free=14)
        self.assertFalse(report["ready"])
        self.assertIn("Host disk", report["problems"][0])

    def test_elasticsearch_on_separate_full_filesystem(self):
        report = self.check(node_free=2)
        self.assertFalse(report["ready"])
        self.assertIn("Elasticsearch node", report["problems"][0])

    def test_green_cluster_can_still_be_write_blocked(self):
        for flag in ("read_only_allow_delete", "read_only", "write", "metadata"):
            for value in (True, "true"):
                with self.subTest(flag=flag, value=value):
                    report = self.check(blocks={flag: value})
                    self.assertFalse(report["ready"])
                    self.assertIn(f"index.blocks.{flag}=true", report["problems"][0])

    def test_unavailable_elasticsearch_fails_closed(self):
        report = self.check(failure=TimeoutError("http://secret@example.org"))
        self.assertFalse(report["ready"])
        self.assertNotIn("secret", str(report))

    def test_disk_limit_boundary(self):
        self.assertTrue(self.check(free=15, node_free=15)["ready"])

    def test_empty_node_response_fails_closed(self):
        with (
            patch.object(
                capacity.shutil, "disk_usage", return_value=SimpleNamespace(total=100, free=20)
            ),
            patch.object(capacity, "get_json", return_value={}),
        ):
            self.assertFalse(capacity.check_capacity("/", "http://localhost", "index")["ready"])
