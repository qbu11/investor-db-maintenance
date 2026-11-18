#!/usr/bin/env python3
"""Cron entry point: API delta sync -> release build -> eval -> gate -> publish.

If any release step fails, the CSV snapshots are restored from the backup the
sync step wrote, so a failed run never leaves a half-promoted dataset.

Exit codes: 0 ok/noop, 2 usage, 24 gate blocked, 25 data, 26 integrity,
28 IO, 30 API, 31 data, 40 lock busy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_investor_release as release_runner  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SYNC_PATH = ROOT / "scripts" / "sync_investors_api.py"
RELEASE_PATH = ROOT / "scripts" / "run_investor_release.py"
DEFAULT_LOCK = ROOT / "research" / ".cron_sync.lock"
DEFAULT_LOG_DIR = ROOT / "research" / "runs"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_stdout(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[-4000:]}
    return payload if isinstance(payload, dict) else {"raw": text[-4000:]}


def script_env() -> dict[str, str]:
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def run_script(command: list[str], timeout: int = 3600) -> tuple[int, dict[str, Any], str]:
    result = subprocess.run(
        command, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        env=script_env(),
    )
    payload = parse_stdout(result.stdout)
    if result.stderr:
        payload.setdefault("stderr", result.stderr[-2000:])
    return result.returncode, payload, result.stdout


def restore_backup(backup: dict[str, Any] | None) -> list[str]:
    if not backup or not backup.get("manifest"):
        return []
    manifest_path = Path(backup["manifest"])
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored: list[str] = []
    for entry in manifest.get("files") or []:
        source = Path(entry["source"])
        backup_path = Path(entry["backup"])
        if not backup_path.is_file():
            continue
        expected = entry.get("sha256")
        if expected and sha256_file(backup_path) != expected:
            raise RuntimeError(f"backup hash mismatch: {backup_path}")
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, source)
        restored.append(str(source))
    return restored


def write_log(log_dir: Path, payload: dict[str, Any]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = log_dir / f"cron_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cron driver for incremental investor database maintenance")
    parser.add_argument("--mode", choices=["fill", "update"], default="update")
    parser.add_argument("--since", default=None)
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--skip-release", action="store_true")
    parser.add_argument("--force-release", action="store_true")
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--include-new", action="store_true")
    parser.add_argument("--keep-backups", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--investors", type=Path, default=None)
    parser.add_argument("--organizations", type=Path, default=None)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--report-dir", type=Path, default=None)
    parser.add_argument("--release-root", type=Path, default=None)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def sync_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable, str(SYNC_PATH),
        "--mode", args.mode,
        "--keep-backups", str(args.keep_backups),
        "--page-size", str(args.page_size),
        "--max-pages", str(args.max_pages),
    ]
    if args.since:
        command.extend(["--since", args.since])
    if args.include_new:
        command.append("--include-new")
    for flag, value in (
        ("--investors", args.investors),
        ("--organizations", args.organizations),
        ("--state", args.state),
        ("--report-dir", args.report_dir),
    ):
        if value is not None:
            command.extend([flag, str(value)])
    if args.dry_run:
        command.append("--dry-run")
    return command


def release_command(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, str(RELEASE_PATH)]
    if args.investors is not None:
        command.extend(["--investors", str(args.investors)])
    if args.organizations is not None:
        command.extend(["--organizations", str(args.organizations)])
    if args.release_root is not None:
        command.extend(["--release-root", str(args.release_root)])
    command.extend(["--log-dir", str(args.log_dir)])
    if args.dry_run:
        command.append("--dry-run")
    return command


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = {
        "started_at": release_runner.common.now_iso(),
        "dry_run": args.dry_run,
        "sync": None,
        "release": None,
        "rolled_back_inputs": [],
    }

    if not args.skip_sync:
        code, sync_payload, _ = run_script(sync_command(args))
        payload["sync"] = sync_payload
        if code != 0:
            payload["status"] = "error"
            payload["stage"] = "sync"
            return code, payload
        changed = int(sync_payload.get("changed_total") or 0)
        if changed == 0 and args.force_release is False and not args.dry_run:
            payload["status"] = "noop"
            payload["finished_at"] = release_runner.common.now_iso()
            return 0, payload
        backup = sync_payload.get("backup")
    else:
        backup = None

    if args.skip_release or args.sync_only:
        payload["status"] = "synced" if not args.skip_sync else "noop"
        payload["finished_at"] = release_runner.common.now_iso()
        return 0, payload

    code, release_payload, _ = run_script(release_command(args), timeout=5400)
    payload["release"] = release_payload
    if code == 0:
        payload["status"] = "ok"
        payload["finished_at"] = release_runner.common.now_iso()
        return 0, payload

    try:
        restored = restore_backup(backup)
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        payload["restore_error"] = str(error)
        restored = []
    payload["rolled_back_inputs"] = restored
    payload["status"] = "rolled_back" if restored else "failed"
    payload["stage"] = "release"
    payload["finished_at"] = release_runner.common.now_iso()
    return code, payload


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        with release_runner.exclusive_lock(args.lock):
            code, payload = run(args)
    except release_runner.LockBusy as error:
        payload = {"status": "error", "stage": "lock", "error": str(error)}
        log_path = write_log(args.log_dir, payload)
        payload["log"] = str(log_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return release_runner.EXIT_LOCK
    except (OSError, subprocess.TimeoutExpired) as error:
        payload = {"status": "error", "stage": "io", "error": str(error)}
        payload["log"] = str(write_log(args.log_dir, payload))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return release_runner.EXIT_IO
    payload.setdefault("finished_at", release_runner.common.now_iso())
    payload["elapsed_seconds"] = round((datetime.now(timezone.utc) - started_fallback(payload)).total_seconds(), 2)
    payload["log"] = str(write_log(args.log_dir, payload))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def started_fallback(payload: dict[str, Any]) -> datetime:
    value = payload.get("started_at")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
