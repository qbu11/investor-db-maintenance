"""Tests for incremental API synchronization and its merge policy."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC_PATH = ROOT / "scripts" / "sync_investors_api.py"

ORG_COLUMNS = [
    "org_id", "chinese_name", "english_name", "full_name", "other_name", "org_description",
    "org_founded_year", "org_aum", "org_investment_stage", "org_sector_focus",
    "org_funding_source", "org_headquarters", "org_notable_portfolio", "org_website",
    "org_people_count", "org_data_confidence", "org_verification_status", "org_sources",
    "research_timestamp", "last_updated",
]

INVESTOR_COLUMNS = [
    "source_user_id", "name", "org", "position", "org_id", "direction",
    "person_expertise", "person_investment_preferences", "person_background",
    "person_sources", "inv_min", "inv_max", "research_timestamp", "last_updated",
]


def load_sync_module():
    spec = importlib.util.spec_from_file_location("investor_sync_test", SYNC_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sync module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, data):
        self.data = data

    def iter_pages(self, path, params=None, page_size=100, max_pages=500):
        items = self.data.get(path, [])
        yield {"page": 1, "total": len(items), "items": items}


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def investor(**overrides):
    row = {
        "source_user_id": "1", "name": "张三", "org": "测试资本", "position": "VP",
        "org_id": "org_aaaa1111", "direction": "AI", "person_expertise": "",
        "person_investment_preferences": "", "person_background": "", "person_sources": "[]",
        "inv_min": "", "inv_max": "", "research_timestamp": "2026-07-27T00:00:00Z",
        "last_updated": "2026-07-28T00:00:00Z",
    }
    row.update(overrides)
    return row


def organization(**overrides):
    row = {
        "org_id": "org_aaaa1111", "chinese_name": "测试资本", "english_name": "Test Capital",
        "org_sector_focus": "AI", "org_sources": "[]",
        "research_timestamp": "2026-07-27T00:00:00Z", "last_updated": "2026-07-28T00:00:00Z",
    }
    row.update(overrides)
    return row


def api_investor(**overrides):
    row = {
        "id": "1", "name": "张三", "job_title": "合伙人", "direction": "AI;SaaS",
        "person_expertise": "机器人", "person_sources": ["https://example.com/a"],
        "updated_at": "2026-09-01T00:00:00Z",
        "fund": {"id": "9", "chinese_name": "测试资本"},
    }
    row.update(overrides)
    return row


def api_org(**overrides):
    row = {
        "id": "9", "chinese_name": "测试资本", "org_sector_focus": ["AI", "SaaS"],
        "org_description": "测试机构", "updated_at": "2026-09-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def make_args(tmp: Path, **overrides) -> argparse.Namespace:
    values = {
        "investors": tmp / "investors.csv",
        "organizations": tmp / "organizations.csv",
        "state": tmp / "state.json",
        "backup_dir": tmp / "backups",
        "report_dir": tmp / "reports",
        "since": None,
        "initial_days": 30,
        "mode": "update",
        "include_new": False,
        "page_size": 100,
        "max_pages": 10,
        "keep_backups": 3,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class SyncInvestorsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sync = load_sync_module()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_csv(self.root / "organizations.csv", ORG_COLUMNS, [organization()])
        write_csv(self.root / "investors.csv", INVESTOR_COLUMNS, [investor()])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_sync(self, data, **overrides):
        return self.sync.run(make_args(self.root, **overrides), client=FakeClient(data))

    def test_update_mode_merges_fields_and_keeps_org_id(self) -> None:
        report = self.run_sync({
            "/fund_investors": [api_investor()],
            "/funds": [api_org()],
        })
        row = read_csv(self.root / "investors.csv")[0]
        self.assertEqual(row["org_id"], "org_aaaa1111")
        self.assertEqual(row["direction"], "AI;SaaS")
        self.assertEqual(row["position"], "合伙人")
        self.assertEqual(row["person_sources"], '["https://example.com/a"]')
        self.assertEqual(row["last_updated"], "2026-09-01T00:00:00Z")
        self.assertEqual(report["changed_total"], 2)

    def test_fill_mode_never_overwrites_non_empty_cells(self) -> None:
        self.run_sync({
            "/fund_investors": [api_investor()],
            "/funds": [api_org()],
        }, mode="fill")
        row = read_csv(self.root / "investors.csv")[0]
        self.assertEqual(row["direction"], "AI")
        self.assertEqual(row["position"], "VP")
        self.assertEqual(row["person_expertise"], "机器人")
        org = read_csv(self.root / "organizations.csv")[0]
        self.assertEqual(org["org_sector_focus"], "AI")
        self.assertEqual(org["org_description"], "测试机构")

    def test_timestamp_only_change_does_not_count_as_release_change(self) -> None:
        report = self.run_sync({
            "/fund_investors": [api_investor(direction="AI", job_title="VP", person_expertise="",
                                             person_sources=[])],
            "/funds": [api_org(org_sector_focus=["AI"], org_description="")],
        })
        self.assertEqual(report["changed_total"], 0)
        self.assertEqual(report["metadata_only_total"], 2)
        row = read_csv(self.root / "investors.csv")[0]
        self.assertEqual(row["last_updated"], "2026-09-01T00:00:00Z")

    def test_new_records_are_skipped_by_default(self) -> None:
        report = self.run_sync({
            "/fund_investors": [api_investor(id="2", name="李四")],
            "/funds": [],
        })
        self.assertEqual(report["counts"]["investors"]["skipped_new"], 1)
        self.assertEqual(len(read_csv(self.root / "investors.csv")), 1)

    def test_include_new_adds_investor_with_existing_org(self) -> None:
        report = self.run_sync({
            "/fund_investors": [api_investor(id="2", name="李四")],
            "/funds": [],
        }, include_new=True)
        rows = read_csv(self.root / "investors.csv")
        self.assertEqual(len(rows), 2)
        added = next(row for row in rows if row["source_user_id"] == "2")
        self.assertEqual(added["org_id"], "org_aaaa1111")
        self.assertEqual(report["changed_total"], 1)

    def test_include_new_generates_stub_org_for_unknown_fund(self) -> None:
        report = self.run_sync({
            "/fund_investors": [api_investor(id="2", name="李四", fund={"id": "99", "chinese_name": "新资本"})],
            "/funds": [],
        }, include_new=True)
        self.assertEqual(report["counts"]["generated_organizations"], 1)
        orgs = read_csv(self.root / "organizations.csv")
        added_org = next(row for row in orgs if row["chinese_name"] == "新资本")
        added_inv = next(row for row in read_csv(self.root / "investors.csv") if row["source_user_id"] == "2")
        self.assertEqual(added_inv["org_id"], added_org["org_id"])

    def test_org_mapping_survives_a_rename(self) -> None:
        self.run_sync({"/fund_investors": [], "/funds": [api_org()]})
        self.run_sync({"/fund_investors": [], "/funds": [api_org(chinese_name="测试资本改名",
                                                                updated_at="2026-10-01T00:00:00Z")]})
        org = read_csv(self.root / "organizations.csv")[0]
        self.assertEqual(org["org_id"], "org_aaaa1111")
        self.assertEqual(org["last_updated"], "2026-10-01T00:00:00Z")

    def test_second_run_is_idempotent(self) -> None:
        data = {"/fund_investors": [api_investor()], "/funds": [api_org()]}
        self.run_sync(data)
        report = self.run_sync(data)
        self.assertEqual(report["changed_total"], 0)
        self.assertEqual(report["metadata_only_total"], 0)

    def test_dry_run_does_not_write(self) -> None:
        before = (self.root / "investors.csv").read_bytes()
        report = self.run_sync({
            "/fund_investors": [api_investor()],
            "/funds": [api_org()],
        }, dry_run=True)
        self.assertEqual(report["changed_total"], 2)
        self.assertEqual((self.root / "investors.csv").read_bytes(), before)
        self.assertFalse((self.root / "state.json").exists())

    def test_backup_contains_original_bytes(self) -> None:
        before = (self.root / "investors.csv").read_bytes()
        report = self.run_sync({
            "/fund_investors": [api_investor()],
            "/funds": [api_org()],
        })
        manifest = json.loads(Path(report["backup"]["manifest"]).read_text(encoding="utf-8"))
        entry = next(item for item in manifest["files"] if item["source"].endswith("investors.csv"))
        self.assertEqual(Path(entry["backup"]).read_bytes(), before)

    def test_old_api_record_is_ignored(self) -> None:
        report = self.run_sync({
            "/fund_investors": [api_investor(updated_at="2026-07-01T00:00:00Z")],
            "/funds": [api_org(updated_at="2026-07-01T00:00:00Z")],
        })
        self.assertEqual(report["changed_total"], 0)
        self.assertEqual(report["counts"]["investors"]["included"], 0)
        self.assertEqual(report["counts"]["organizations"]["included"], 0)

    def test_timestamp_formats_compare_by_instant(self) -> None:
        row = organization(research_timestamp="2026-07-27T15:21:21+08:00")
        api = api_org(research_timestamp="2026-07-27T07:21:21Z")
        self.assertTrue(self.sync.values_equal(row["research_timestamp"], api["research_timestamp"], "timestamp"))

    def test_semicolon_values_compare_as_sets(self) -> None:
        self.assertTrue(self.sync.values_equal("AI;SaaS", "SaaS;AI", "semicolon"))


if __name__ == "__main__":
    unittest.main()
