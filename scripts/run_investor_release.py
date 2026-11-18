#!/usr/bin/env python3
"""Build, evaluate, gate and publish an investor-search release.

The release is built from the current CSV snapshots plus every research batch
already marked applied in ``research/batch_registry.csv``. Publishing switches
``releases/current.json`` only when the regression gate passes.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_common as common  # noqa: E402
import release_gate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILD_PATH = ROOT / "scripts" / "build_investor_search_v2.py"
EVAL_PATH = ROOT / "scripts" / "eval_investor_search_v2.py"
MANAGE_PATH = ROOT / "scripts" / "manage_release.py"
def _default_snapshot(stable_name: str, legacy_name: str) -> Path:
    stable = ROOT / "output" / stable_name
    return stable if stable.is_file() else ROOT / "output" / legacy_name


DEFAULT_INVESTORS = _default_snapshot("investors.csv", "investors_enriched_20260728_1745.csv")
DEFAULT_ORGANIZATIONS = _default_snapshot("organizations.csv", "organizations_enriched_20260728_1745.csv")
DEFAULT_TAGS = ROOT / "doc" / "投资人数据库规范标签表_v2.csv"
DEFAULT_TERMS = ROOT / "doc" / "investor_search_terms_v2.csv"
DEFAULT_QUERIES = ROOT / "doc" / "investor_search_acceptance_queries_v3.csv"
DEFAULT_POLICY = ROOT / "doc" / "investor_search_release_policy_v3.json"
DEFAULT_RELEASE_ROOT = ROOT / "releases"
DEFAULT_LOCK = ROOT / "releases" / ".release.lock"
DEFAULT_LOG_DIR = ROOT / "research" / "runs"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GATE = 24
EXIT_DATA = 25
EXIT_INTEGRITY = 26
EXIT_IO = 28
EXIT_LOCK = 40
LOCK_STALE_SECONDS = 6 * 3600


class LockBusy(RuntimeError):
    pass


@contextmanager
def exclusive_lock(path: Path, stale_seconds: int = LOCK_STALE_SECONDS) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _ in range(2):
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, json.dumps({
                "pid": os.getpid(),
                "started_at": common.now_iso(),
            }).encode("utf-8"))
            os.close(descriptor)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0
            if age > stale_seconds:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            raise LockBusy(f"another release is running (lock: {path})")
    if not acquired:
        raise LockBusy(f"another release is running (lock: {path})")
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def release_major(name: str) -> int | None:
    match = re.match(r"^v(\d+)_", name)
    return int(match.group(1)) if match else None


def next_release_id(release_root: Path) -> str:
    majors = [release_major(path.name) for path in release_root.iterdir() if path.is_dir()] if release_root.is_dir() else []
    pointer = common.read_pointer()
    if pointer and pointer.get("active_release_id"):
        majors.append(release_major(str(pointer["active_release_id"])))
    existing = [value for value in majors if value is not None]
    major = (max(existing) + 1) if existing else 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return f"v{major}_{stamp}"


def collect_evidence(registry_path: Path, batches_dir: Path, forced: list[str]) -> tuple[list[Path], list[str]]:
    batch_ids: list[str] = []
    if registry_path.is_file():
        with registry_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("status") in {"applied", "validated"} and row.get("batch_id"):
                    batch_ids.append(row["batch_id"].strip())
    batch_ids.extend(value.strip() for value in forced if value.strip())
    files: list[Path] = []
    missing: list[str] = []
    for identifier in dict.fromkeys(batch_ids):
        candidate = batches_dir / identifier / "normalized_records.csv"
        if candidate.is_file():
            files.append(candidate)
        else:
            missing.append(identifier)
    return files, missing


def resolve_baseline(args: argparse.Namespace) -> Path | None:
    if args.baseline:
        return args.baseline
    pointer = common.read_pointer()
    if pointer and pointer.get("release_dir"):
        candidate = ROOT / pointer["release_dir"] / "eval_report.json"
        if candidate.is_file():
            return candidate
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    relative = policy.get("baseline_report")
    if relative:
        candidate = ROOT / relative
        if candidate.is_file():
            return candidate
    return None


def script_env() -> dict[str, str]:
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def run_eval(release_dir: Path, queries: Path, top_k: int, output: Path) -> tuple[int, str]:
    command = [
        sys.executable, str(EVAL_PATH),
        "--queries", str(queries),
        "--release-dir", str(release_dir),
        "--top-k", str(top_k),
        "--output", str(output),
    ]
    result = subprocess.run(
        command, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        timeout=1800, env=script_env(),
    )
    return result.returncode, (result.stderr or result.stdout or "")[-4000:]


def publish_release(
    release_dir: Path,
    eval_report: Path,
    policy: Path,
    baseline: Path | None,
    mode: str,
    allow_regression: bool,
) -> dict[str, Any]:
    command = [
        sys.executable, str(MANAGE_PATH), "publish",
        "--release-dir", str(release_dir),
        "--eval-report", str(eval_report),
        "--policy", str(policy),
        "--mode", mode,
    ]
    if baseline:
        command.extend(["--baseline", str(baseline)])
    if allow_regression:
        command.append("--allow-regression")
    result = subprocess.run(
        command, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        timeout=600, env=script_env(),
    )
    payload: Any = None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"raw_stdout": result.stdout[-4000:], "raw_stderr": result.stderr[-4000:]}
    return {"returncode": result.returncode, "payload": payload}


def evaluate_gate(
    release_dir: Path,
    eval_report: Path,
    policy: Path,
    baseline: Path | None,
    mode: str,
) -> dict[str, Any]:
    gate = release_gate.run(policy, eval_report, baseline, mode)
    (release_dir / "gate_report.json").write_text(
        json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return gate


def write_log(log_dir: Path, payload: dict[str, Any]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = log_dir / f"release_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build, evaluate and publish an investor-search release")
    parser.add_argument("--investors", type=Path, default=DEFAULT_INVESTORS)
    parser.add_argument("--organizations", type=Path, default=DEFAULT_ORGANIZATIONS)
    parser.add_argument("--tags", type=Path, default=DEFAULT_TAGS)
    parser.add_argument("--terms", type=Path, default=DEFAULT_TERMS)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--release-id", default=None)
    parser.add_argument("--release-root", type=Path, default=DEFAULT_RELEASE_ROOT)
    parser.add_argument("--evidence-batch", action="append", default=[])
    parser.add_argument("--eval-top-k", type=int, default=50)
    parser.add_argument("--mode", choices=["regression", "release"], default="regression")
    parser.add_argument("--allow-regression", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lock", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    return parser


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    started = time.time()
    release_id = args.release_id or next_release_id(args.release_root)
    release_dir = args.release_root / release_id
    if release_dir.exists() and any(release_dir.iterdir()):
        return EXIT_USAGE, {"status": "error", "error": f"release directory is not empty: {release_dir}"}
    evidence, missing = collect_evidence(
        ROOT / "research" / "batch_registry.csv",
        ROOT / "research" / "batches",
        args.evidence_batch,
    )
    pointer = common.read_pointer() or {}
    parent_id = pointer.get("active_release_id")
    lock_path = args.lock or (args.release_root / ".release.lock")

    with exclusive_lock(lock_path):
        build = load_module("investor_search_build_release_runner", BUILD_PATH)
        build_args = argparse.Namespace(
            investors=args.investors,
            organizations=args.organizations,
            tags=args.tags,
            terms=args.terms,
            output_dir=release_dir,
            overwrite=False,
            release_id=release_id,
            parent_release_id=parent_id,
            evidence_files=[str(path) for path in evidence],
        )
        try:
            manifest = build.build_release(build_args)
        except (SystemExit, OSError, csv.Error, json.JSONDecodeError, ValueError) as error:
            message = str(error) or getattr(error, "code", "build failed")
            payload = {
                "status": "error", "stage": "build", "release_id": release_id,
                "error": message, "release_dir": str(release_dir),
            }
            payload["log"] = str(write_log(args.log_dir, payload))
            return EXIT_DATA, payload

        eval_report = release_dir / "eval_report.json"
        eval_code, eval_tail = run_eval(release_dir, args.queries, args.eval_top_k, eval_report)
        if eval_code != 0 or not eval_report.is_file():
            payload = {
                "status": "error", "stage": "eval", "release_id": release_id,
                "error": f"evaluation failed with exit code {eval_code}",
                "output_tail": eval_tail, "release_dir": str(release_dir),
            }
            payload["log"] = str(write_log(args.log_dir, payload))
            return EXIT_DATA, payload

        baseline = resolve_baseline(args)
        if args.no_publish or args.dry_run:
            gate = evaluate_gate(release_dir, eval_report, args.policy, baseline, args.mode)
            status = "dry_run" if args.dry_run else "built"
            payload = {
                "status": status, "release_id": release_id, "release_dir": str(release_dir),
                "parent_release_id": parent_id,
                "evidence_batches": [path.parent.name for path in evidence],
                "missing_evidence_batches": missing,
                "gate": gate, "baseline": str(baseline) if baseline else None,
                "eval_report": str(eval_report), "manifest_counts": manifest.get("counts", {}),
                "elapsed_seconds": round(time.time() - started, 2),
            }
            payload["log"] = str(write_log(args.log_dir, payload))
            if gate.get("status") != "pass" and not args.allow_regression:
                payload["status"] = "blocked"
                return EXIT_GATE, payload
            return EXIT_OK, payload

        published = publish_release(release_dir, eval_report, args.policy, baseline, args.mode, args.allow_regression)
        payload = {
            "status": "published" if published["returncode"] == 0 else "blocked",
            "release_id": release_id, "release_dir": str(release_dir),
            "parent_release_id": parent_id,
            "evidence_batches": [path.parent.name for path in evidence],
            "missing_evidence_batches": missing,
            "baseline": str(baseline) if baseline else None,
            "eval_report": str(eval_report), "manifest_counts": manifest.get("counts", {}),
            "publish": published["payload"],
            "elapsed_seconds": round(time.time() - started, 2),
        }
        payload["log"] = str(write_log(args.log_dir, payload))
        if published["returncode"] == 0:
            return EXIT_OK, payload
        if published["returncode"] == EXIT_GATE:
            return EXIT_GATE, payload
        return published["returncode"] or EXIT_IO, payload


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        code, payload = run(args)
    except LockBusy as error:
        print(json.dumps({"status": "error", "stage": "lock", "error": str(error)}, ensure_ascii=False))
        return EXIT_LOCK
    except (OSError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "error", "stage": "io", "error": str(error)}, ensure_ascii=False))
        return EXIT_IO
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
