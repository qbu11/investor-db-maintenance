#!/usr/bin/env python3
"""Publish, roll back and inspect investor-search releases.

Publishing is gated: manifest hashes are verified, the eval report must pass
the policy gate, and only then is the pointer switched atomically.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_common as common  # noqa: E402
import release_gate  # noqa: E402


def update_batch_registry(batch_ids: list[str], status: str) -> None:
    path = common.RESEARCH_DIR / "batch_registry.csv"
    if not path.is_file() or not batch_ids:
        return
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0].keys()) if rows else []
    for row in rows:
        if row.get("batch_id") in batch_ids:
            row["status"] = status
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def publish(args: argparse.Namespace) -> int:
    release_dir = args.release_dir.resolve()
    if not release_dir.is_dir():
        print(json.dumps({"status": "error", "error": f"release dir not found: {release_dir}"}, ensure_ascii=False))
        return common.EXIT_USAGE
    violations = common.verify_manifest(release_dir)
    if violations:
        print(json.dumps({"status": "error", "error": "manifest integrity failed", "violations": violations},
                         ensure_ascii=False))
        return common.EXIT_INTEGRITY
    manifest = common.load_manifest(release_dir)
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    baseline_path = args.baseline
    if baseline_path is None:
        relative = policy.get("baseline_report")
        baseline_path = (common.ROOT / relative) if relative else None
    gate = release_gate.run(args.policy, args.eval_report, baseline_path, args.mode)
    gate_path = release_dir / "gate_report.json"
    gate_path.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    if gate["status"] != "pass" and not args.allow_regression:
        print(json.dumps({"status": "blocked", "gate": gate}, ensure_ascii=False, indent=2))
        return common.EXIT_GATE

    previous = common.read_pointer()
    if previous:
        previous_id = previous.get("active_release_id")
        previous_dir = previous.get("release_dir")
    else:
        previous_id = "v2_20261118"
        previous_dir = "output_v2"
    pointer = {
        "pointer_version": "1.0",
        "active_release_id": manifest.get("release_id") or args.release_id or release_dir.name,
        "release_dir": release_dir.relative_to(common.ROOT).as_posix(),
        "previous_release_id": previous_id,
        "previous_release_dir": previous_dir,
        "batch_ids": [batch.get("batch_id") for batch in manifest.get("research_batches") or []],
        "promoted_at": common.now_iso(),
        "eval_report_sha256": common.sha256_file(args.eval_report),
        "gate_status": gate["status"],
        "gate_mode": args.mode,
        "status": "active",
    }
    common.write_pointer(pointer)
    common.append_history({"event": "publish", **pointer})
    update_batch_registry(pointer["batch_ids"], "applied")
    print(json.dumps({"status": "published", "pointer": pointer, "gate": gate}, ensure_ascii=False, indent=2))
    return common.EXIT_OK


def resolve_previous(pointer: dict) -> tuple[str, Path] | None:
    release_id = pointer.get("previous_release_id")
    release_dir = pointer.get("previous_release_dir")
    if release_id and release_dir:
        path = common.ROOT / release_dir
        if path.is_dir():
            return release_id, path
    if (common.ROOT / "output_v2").is_dir():
        return "v2_20261118", common.ROOT / "output_v2"
    return None


def rollback(args: argparse.Namespace) -> int:
    pointer = common.read_pointer()
    if pointer is None:
        print(json.dumps({"status": "error", "error": "no active release pointer"}, ensure_ascii=False))
        return common.EXIT_USAGE
    if args.to:
        target_id = args.to
        target_dir = common.RELEASES_DIR / target_id
        if not target_dir.is_dir():
            target_dir = common.ROOT / "output_v2"
        if not target_dir.is_dir():
            print(json.dumps({"status": "error", "error": f"release not found: {target_id}"}, ensure_ascii=False))
            return common.EXIT_INTEGRITY
    else:
        previous = resolve_previous(pointer)
        if previous is None:
            print(json.dumps({"status": "error", "error": "no previous release to roll back to"}, ensure_ascii=False))
            return common.EXIT_USAGE
        target_id, target_dir = previous
    event = {
        "pointer_version": "1.0",
        "active_release_id": target_id,
        "release_dir": target_dir.relative_to(common.ROOT).as_posix(),
        "previous_release_id": pointer.get("active_release_id"),
        "previous_release_dir": pointer.get("release_dir"),
        "promoted_at": common.now_iso(),
        "gate_status": "rolled_back",
        "gate_mode": "rollback",
        "batch_ids": pointer.get("batch_ids") or [],
        "status": "active",
    }
    common.write_pointer(event)
    common.append_history({"event": "rollback", **event})
    print(json.dumps({"status": "rolled_back", "pointer": event}, ensure_ascii=False, indent=2))
    return common.EXIT_OK


def status(args: argparse.Namespace) -> int:
    pointer = common.read_pointer()
    history: list[dict] = []
    if common.HISTORY_PATH.is_file():
        history = [json.loads(line) for line in common.HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(json.dumps({"pointer": pointer, "recent_history": history[-5:]}, ensure_ascii=False, indent=2))
    return common.EXIT_OK


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage investor-search releases")
    sub = parser.add_subparsers(dest="command", required=True)

    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--release-dir", type=Path, required=True)
    publish_parser.add_argument("--release-id", default=None)
    publish_parser.add_argument("--eval-report", type=Path, required=True)
    publish_parser.add_argument("--policy", type=Path, default=common.ROOT / "doc" / "investor_search_release_policy_v3.json")
    publish_parser.add_argument("--baseline", type=Path, default=None)
    publish_parser.add_argument("--mode", choices=["regression", "release"], default="regression")
    publish_parser.add_argument("--allow-regression", action="store_true")

    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("--to", default=None, help="release id under releases/ or v2_20261118")

    sub.add_parser("status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "publish":
        return publish(args)
    if args.command == "rollback":
        return rollback(args)
    return status(args)


if __name__ == "__main__":
    raise SystemExit(main())
