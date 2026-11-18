"""Tests for release orchestration helpers and the cron rollback guard."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = load_module("investor_release_runner_test", ROOT / "scripts" / "run_investor_release.py")
cron = load_module("investor_cron_sync_test", ROOT / "scripts" / "cron_investor_sync.py")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReleaseRunnerTests(unittest.TestCase):
    def test_collect_evidence_filters_status_and_reports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batches = root / "batches"
            registry = root / "batch_registry.csv"
            rows = [
                {"batch_id": "b_applied", "status": "applied"},
                {"batch_id": "b_validated", "status": "validated"},
                {"batch_id": "b_held", "status": "held"},
                {"batch_id": "b_missing", "status": "applied"},
            ]
            with registry.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["batch_id", "status"], lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            for batch_id in ("b_applied", "b_validated", "b_held"):
                target = batches / batch_id
                target.mkdir(parents=True)
                (target / "normalized_records.csv").write_text("x\n", encoding="utf-8")
            files, missing = runner.collect_evidence(registry, batches, ["b_forced"])
            self.assertEqual([path.parent.name for path in files], ["b_applied", "b_validated"])
            self.assertEqual(missing, ["b_missing", "b_forced"])

    def test_next_release_id_increments_major(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("v3_20260101", "v5_20260101"):
                (root / name).mkdir()
            self.assertTrue(runner.next_release_id(root).startswith("v6_"))

    def test_exclusive_lock_blocks_second_holder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.lock"
            with runner.exclusive_lock(path):
                with self.assertRaises(runner.LockBusy):
                    with runner.exclusive_lock(path):
                        pass
            self.assertFalse(path.exists())

    def test_restore_backup_restores_verified_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.csv"
            backup = root / "backup.csv"
            source.write_text("new\n", encoding="utf-8")
            backup.write_text("old\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "files": [{"source": str(source), "backup": str(backup), "sha256": sha256(backup)}]
            }), encoding="utf-8")
            restored = cron.restore_backup({"manifest": str(manifest)})
            self.assertEqual(restored, [str(source)])
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")

    def test_restore_backup_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.csv"
            backup = root / "backup.csv"
            source.write_text("new\n", encoding="utf-8")
            backup.write_text("tampered\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "files": [{"source": str(source), "backup": str(backup), "sha256": "0" * 64}]
            }), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                cron.restore_backup({"manifest": str(manifest)})
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")

    def test_restore_backup_without_manifest_is_noop(self) -> None:
        self.assertEqual(cron.restore_backup(None), [])
        self.assertEqual(cron.restore_backup({}), [])

    def test_cron_rolls_back_inputs_when_release_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "investors.csv"
            backup = root / "investors.backup.csv"
            source.write_text("new\n", encoding="utf-8")
            backup.write_text("old\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "files": [{"source": str(source), "backup": str(backup), "sha256": sha256(backup)}]
            }), encoding="utf-8")
            sync_payload = {"status": "ok", "changed_total": 1, "backup": {"manifest": str(manifest)}}
            release_payload = {"status": "blocked"}
            with mock.patch.object(
                cron, "run_script", side_effect=[(0, sync_payload, ""), (24, release_payload, "")]
            ):
                code, payload = cron.run(cron_args(root))
            self.assertEqual(code, 24)
            self.assertEqual(payload["status"], "rolled_back")
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")


def cron_args(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        mode="update", since=None, skip_sync=False, skip_release=False, force_release=False,
        sync_only=False, include_new=False, keep_backups=3, page_size=100, max_pages=10,
        investors=root / "investors.csv", organizations=root / "organizations.csv",
        state=root / "state.json", report_dir=root / "reports", release_root=root / "releases",
        lock=root / "cron.lock", log_dir=root / "runs", dry_run=False,
    )


if __name__ == "__main__":
    unittest.main()
