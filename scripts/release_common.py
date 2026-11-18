#!/usr/bin/env python3
"""Shared release utilities: manifest discovery, hashes, pointer files, exit codes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RELEASES_DIR = ROOT / "releases"
POINTER_PATH = RELEASES_DIR / "current.json"
HISTORY_PATH = RELEASES_DIR / "history.jsonl"
RESEARCH_DIR = ROOT / "research"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GATE = 24
EXIT_DATA = 25
EXIT_INTEGRITY = 26


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_manifest(release_dir: Path) -> Path:
    manifests = sorted(release_dir.glob("investor_search_*_manifest.json"))
    if not manifests:
        raise FileNotFoundError(f"no release manifest in {release_dir}")
    return max(manifests, key=lambda path: path.stat().st_mtime)


def load_manifest(release_dir: Path) -> dict[str, Any]:
    return json.loads(find_manifest(release_dir).read_text(encoding="utf-8"))


def verify_manifest(release_dir: Path) -> list[str]:
    """Return a list of integrity violations (empty means OK)."""
    manifest = load_manifest(release_dir)
    violations: list[str] = []
    for filename, metadata in (manifest.get("outputs") or {}).items():
        path = release_dir / filename
        if not path.is_file():
            violations.append(f"missing artifact: {filename}")
            continue
        if sha256_file(path) != metadata.get("sha256"):
            violations.append(f"hash mismatch: {filename}")
    return violations


def read_pointer() -> dict[str, Any] | None:
    if not POINTER_PATH.is_file():
        return None
    try:
        return json.loads(POINTER_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_pointer(payload: dict[str, Any]) -> None:
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=RELEASES_DIR, prefix="current.", suffix=".tmp", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, POINTER_PATH)


def append_history(event: dict[str, Any]) -> None:
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summary_of(report: dict[str, Any]) -> dict[str, Any]:
    return report.get("summary") or {}


def category_of(report: dict[str, Any], name: str) -> dict[str, Any]:
    return (report.get("by_category") or {}).get(name) or {}


def resolve_release_dir(release_id: str | None = None) -> Path:
    if release_id:
        candidate = RELEASES_DIR / release_id
        if not candidate.is_dir():
            raise FileNotFoundError(f"release not found: {candidate}")
        return candidate
    pointer = read_pointer()
    if pointer and pointer.get("release_dir"):
        candidate = ROOT / pointer["release_dir"]
        if candidate.is_dir():
            return candidate
    return ROOT / "output_v2"
