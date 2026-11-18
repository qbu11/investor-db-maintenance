#!/usr/bin/env python3
"""Merge incremental MiraclePlus API updates into the local investor snapshots.

Policy:
- Only rows already present locally are updated unless ``--include-new`` is set.
- A record is considered only when its API ``updated_at`` is newer than the
  local row ``last_updated``.
- ``--mode fill`` only fills empty local cells. ``--mode update`` also replaces
  differing non-empty cells. Local values are never cleared.
- Identity columns (investor name, organization names, org_id) are never
  rewritten by the API; differences are reported as conflicts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import investor_api_client as api_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVESTORS = ROOT / "output" / "investors_enriched_20260728_1745.csv"
DEFAULT_ORGANIZATIONS = ROOT / "output" / "organizations_enriched_20260728_1745.csv"
DEFAULT_STATE = ROOT / "research" / "api_sync_state.json"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
DEFAULT_REPORT_DIR = ROOT / "research" / "api_sync"

INVESTOR_ID = "source_user_id"
ORG_ID = "org_id"
INVESTOR_ENDPOINT = "/fund_investors"
ORG_ENDPOINT = "/funds"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_API = 30
EXIT_DATA = 31
EXIT_IO = 28
SYNC_STATE_VERSION = "1.0"
REPORT_CHANGE_LIMIT = 500


@dataclass(frozen=True)
class FieldSpec:
    column: str
    source: str
    kind: str = "text"
    identity: bool = False


INVESTOR_FIELDS = (
    FieldSpec("name", "name", identity=True),
    FieldSpec("position", "job_title"),
    FieldSpec("currency", "currency"),
    FieldSpec("direction", "direction"),
    FieldSpec("person_background", "person_background"),
    FieldSpec("person_representative_deals", "person_representative_deals"),
    FieldSpec("person_investment_preferences", "person_investment_preferences"),
    FieldSpec("person_linkedin", "person_linkedin"),
    FieldSpec("person_education", "person_education"),
    FieldSpec("person_education_detail", "person_education_detail"),
    FieldSpec("person_work_experience", "person_work_experience"),
    FieldSpec("person_expertise", "person_expertise"),
    FieldSpec("person_data_confidence", "person_data_confidence"),
    FieldSpec("person_verification_status", "person_verification_status"),
    FieldSpec("person_sources", "person_sources", "json"),
    FieldSpec("deal_count_verified", "deal_count_verified", "number"),
    FieldSpec("inv_range", "inv_range"),
    FieldSpec("inv_min", "inv_min", "number"),
    FieldSpec("inv_max", "inv_max", "number"),
    FieldSpec("compliance_name_match", "compliance_name_match"),
    FieldSpec("compliance_org_match", "compliance_org_match"),
    FieldSpec("compliance_position_match", "compliance_position_match"),
    FieldSpec("compliance_role_match", "compliance_role_match"),
    FieldSpec("compliance_deal_match", "compliance_deal_match"),
    FieldSpec("compliance_overall", "compliance_overall"),
    FieldSpec("compliance_detail", "compliance_detail"),
    FieldSpec("search_summary", "search_summary"),
    FieldSpec("research_notes", "research_notes"),
    FieldSpec("research_timestamp", "research_timestamp", "timestamp"),
)

ORG_FIELDS = (
    FieldSpec("chinese_name", "chinese_name", identity=True),
    FieldSpec("english_name", "english_name", identity=True),
    FieldSpec("full_name", "full_name", identity=True),
    FieldSpec("other_name", "other_name", identity=True),
    FieldSpec("org_description", "org_description"),
    FieldSpec("org_founded_year", "org_founded_year", "number"),
    FieldSpec("org_aum", "org_aum"),
    FieldSpec("org_investment_stage", "org_investment_stage"),
    FieldSpec("org_sector_focus", "org_sector_focus", "semicolon"),
    FieldSpec("org_funding_source", "org_funding_source"),
    FieldSpec("org_headquarters", "org_headquarters"),
    FieldSpec("org_notable_portfolio", "org_notable_portfolio", "semicolon"),
    FieldSpec("org_website", "org_website"),
    FieldSpec("org_people_count", "org_people_count", "number"),
    FieldSpec("org_data_confidence", "org_data_confidence"),
    FieldSpec("org_verification_status", "org_verification_status"),
    FieldSpec("org_sources", "org_sources", "json"),
    FieldSpec("research_timestamp", "research_timestamp", "timestamp"),
)


def normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def normalized_key(value: Any) -> str:
    return api_client.normalized_text(value)


def value_is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return not str(value).strip()


def serialize(value: Any, kind: str) -> str:
    if value_is_empty(value):
        return ""
    if kind == "json":
        if isinstance(value, (list, tuple)):
            return json.dumps(list(value), ensure_ascii=False)
        return normalize(value)
    if kind == "semicolon":
        if isinstance(value, (list, tuple)):
            return ";".join(normalize(item) for item in value if normalize(item))
        return normalize(value)
    if kind == "number":
        if isinstance(value, bool):
            return ""
        if isinstance(value, (int, float)):
            return str(value)
        return normalize(value)
    return normalize(value)


def values_equal(left: Any, right: Any, kind: str) -> bool:
    left_text = normalize(left)
    right_text = normalize(right)
    if not left_text or not right_text:
        return left_text == right_text
    if kind == "number":
        try:
            return Decimal(left_text) == Decimal(right_text)
        except (InvalidOperation, ValueError):
            return left_text == right_text
    if kind == "semicolon":
        left_items = {normalized_key(item) for item in left_text.split(";") if item.strip()}
        right_items = {normalized_key(item) for item in right_text.split(";") if item.strip()}
        return left_items == right_items
    if kind == "timestamp":
        left_dt = api_client.parse_datetime(left_text)
        right_dt = api_client.parse_datetime(right_text)
        if left_dt is not None and right_dt is not None:
            return left_dt == right_dt
        return left_text == right_text
    return normalized_key(left_text) == normalized_key(right_text)


def stable_org_id(name: str) -> str:
    digest = hashlib.sha256(normalize(name).encode("utf-8")).hexdigest()[:8]
    return f"org_{digest}"


def read_csv_file(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    return rows, fields


def write_csv_atomic(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8-sig", newline="", dir=str(path.parent),
        prefix=f"{path.name}.", suffix=".tmp", delete=False,
    )
    try:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent),
        prefix=f"{path.name}.", suffix=".tmp", delete=False,
    )
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_timestamp(rows: list[dict[str, str]]) -> datetime | None:
    timestamps = [
        parsed for parsed in (api_client.parse_datetime(row.get("last_updated")) for row in rows)
        if parsed is not None
    ]
    return max(timestamps) if timestamps else None


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"state_version": SYNC_STATE_VERSION, "streams": {}, "org_mapping": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"state file is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("state file must contain a JSON object")
    payload.setdefault("streams", {})
    payload.setdefault("org_mapping", {})
    return payload


def resolve_since(args: argparse.Namespace, streams: dict[str, Any], rows: list[dict[str, str]]) -> datetime:
    if args.since:
        parsed = api_client.parse_datetime(args.since)
        if parsed is None:
            raise ValueError("--since must be an ISO8601 timestamp with timezone")
        return parsed
    stream_since = (streams.get(args.stream) or {}).get("last_sync")
    parsed_stream = api_client.parse_datetime(stream_since)
    if parsed_stream:
        return parsed_stream
    snapshot = latest_timestamp(rows)
    if snapshot:
        return snapshot - timedelta(seconds=1)
    return datetime.now(timezone.utc) - timedelta(days=args.initial_days)


def build_org_index(org_rows: list[dict[str, str]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for row in org_rows:
        identifier = normalize(row.get(ORG_ID))
        if not identifier:
            continue
        for name in (row.get("chinese_name"), row.get("full_name"), row.get("english_name"), row.get("other_name")):
            key = normalized_key(name)
            if key and identifier not in index[key]:
                index[key].append(identifier)
    return {key: values for key, values in index.items() if values}


def resolve_org_id(fund: dict[str, Any], index: dict[str, list[str]], mapping: dict[str, str]) -> tuple[str | None, str]:
    api_id = normalize(fund.get("id"))
    if api_id and mapping.get(api_id):
        return mapping[api_id], "mapping"
    for field in ("chinese_name", "full_name", "english_name", "other_name"):
        candidates = index.get(normalized_key(fund.get(field))) or []
        if len(candidates) == 1:
            return candidates[0], f"name:{field}"
        if len(candidates) > 1:
            return None, "ambiguous"
    return None, "unmatched"


def merge_fields(
    row: dict[str, str],
    item: dict[str, Any],
    specs: tuple[FieldSpec, ...],
    mode: str,
) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    updated = dict(row)
    changes: dict[str, dict[str, str]] = {}
    conflicts: dict[str, dict[str, str]] = {}
    for spec in specs:
        new_value = serialize(item.get(spec.source), spec.kind)
        if not new_value:
            continue
        old_value = normalize(row.get(spec.column))
        if not old_value:
            updated[spec.column] = new_value
            changes[spec.column] = {"old": "", "new": new_value[:300]}
            continue
        if values_equal(old_value, new_value, spec.kind):
            continue
        if spec.identity or mode != "update":
            conflicts[spec.column] = {"kept": old_value[:300], "api": new_value[:300]}
            continue
        updated[spec.column] = new_value
        changes[spec.column] = {"old": old_value[:300], "new": new_value[:300]}
    return updated, changes, conflicts


def new_row_from_item(item: dict[str, Any], identifier_field: str, specs: tuple[FieldSpec, ...]) -> dict[str, str]:
    row = {identifier_field: normalize(item.get("id"))}
    for spec in specs:
        value = serialize(item.get(spec.source), spec.kind)
        if value:
            row[spec.column] = value
    updated = api_client.parse_datetime(item.get("updated_at"))
    if updated:
        row["last_updated"] = api_client.to_utc_iso(updated)
    return row


def should_apply(row: dict[str, str], item: dict[str, Any]) -> bool:
    api_updated = api_client.parse_datetime(item.get("updated_at"))
    if api_updated is None:
        return False
    local_updated = api_client.parse_datetime(row.get("last_updated"))
    return local_updated is None or api_updated > local_updated


def merge_records(
    rows: list[dict[str, str]],
    items: list[dict[str, Any]],
    identifier_field: str,
    specs: tuple[FieldSpec, ...],
    mode: str,
    include_new: bool,
    new_identifier: Callable[[dict[str, Any]], str | None] | None = None,
    lookup_identifier: Callable[[dict[str, Any]], str | None] | None = None,
    decorate_new: Callable[[dict[str, str], dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Merge API items into CSV rows and return rows plus a change report."""
    index = {normalize(row.get(identifier_field)): position for position, row in enumerate(rows)}
    output = list(rows)
    changes: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    unmatched: list[dict[str, str]] = []

    for item in items:
        api_id = normalize(item.get("id"))
        if not api_id:
            counters["skipped_invalid"] += 1
            continue
        identifier = lookup_identifier(item) if lookup_identifier else api_id
        if identifier is None:
            counters["skipped_unresolved"] += 1
            unmatched.append({"id": api_id, "reason": "unresolved"})
            continue
        position = index.get(identifier)
        if position is None:
            if not include_new:
                counters["skipped_new"] += 1
                continue
            local_id = new_identifier(item) if new_identifier else identifier
            if not local_id:
                counters["skipped_unresolved"] += 1
                unmatched.append({"id": api_id, "reason": "unresolved"})
                continue
            new_row = new_row_from_item(item, identifier_field, specs)
            new_row[identifier_field] = local_id
            if decorate_new is not None and not decorate_new(new_row, item):
                counters["skipped_unresolved"] += 1
                unmatched.append({"id": api_id, "reason": "decorate_failed"})
                continue
            index[local_id] = len(output)
            output.append(new_row)
            counters["added"] += 1
            changes.append({"id": local_id, "fields": sorted(key for key, value in new_row.items() if value)})
            continue
        row = output[position]
        if not should_apply(row, item):
            counters["skipped_old"] += 1
            continue
        updated, field_changes, field_conflicts = merge_fields(row, item, specs, mode)
        api_updated = api_client.parse_datetime(item.get("updated_at"))
        if api_updated is not None:
            updated["last_updated"] = api_client.to_utc_iso(api_updated)
        if updated == row:
            counters["unchanged"] += 1
            continue
        if not field_changes:
            counters["timestamp_only"] += 1
            output[position] = updated
            continue
        counters["updated"] += 1
        output[position] = updated
        changes.append({"id": identifier, "fields": sorted(field_changes)})
        if field_conflicts:
            conflicts.append({"id": identifier, "fields": field_conflicts})

    return {
        "rows": output,
        "counters": dict(counters),
        "changes": changes[:REPORT_CHANGE_LIMIT],
        "change_count": len(changes),
        "conflicts": conflicts[:REPORT_CHANGE_LIMIT],
        "conflict_count": len(conflicts),
        "unmatched": unmatched[:REPORT_CHANGE_LIMIT],
    }


def create_backup(paths: list[Path], backup_dir: Path, run_id: str) -> dict[str, Any]:
    target = backup_dir / run_id
    target.mkdir(parents=True, exist_ok=True)
    files = []
    for source in paths:
        destination = target / source.name
        shutil.copy2(source, destination)
        files.append({
            "source": str(source.resolve()),
            "backup": str(destination.resolve()),
            "sha256": sha256_file(source),
        })
    manifest = {"run_id": run_id, "created_at": api_client.to_utc_iso(datetime.now(timezone.utc)), "files": files}
    write_json_atomic(target / "manifest.json", manifest)
    return {"dir": str(target.resolve()), "manifest": str((target / "manifest.json").resolve()), "files": files}


def prune_backups(backup_dir: Path, keep: int) -> list[str]:
    if keep < 1 or not backup_dir.is_dir():
        return []
    candidates = sorted((path for path in backup_dir.iterdir() if path.is_dir()), key=lambda path: path.name)
    removed = []
    for path in candidates[:-keep]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(str(path))
    return removed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incrementally sync investor data from the MiraclePlus API")
    parser.add_argument("--investors", type=Path, default=DEFAULT_INVESTORS)
    parser.add_argument("--organizations", type=Path, default=DEFAULT_ORGANIZATIONS)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--since", default=None, help="ISO8601 cutoff; defaults to the state file or snapshot time")
    parser.add_argument("--initial-days", type=int, default=30)
    parser.add_argument("--mode", choices=["fill", "update"], default="update")
    parser.add_argument("--include-new", action="store_true")
    parser.add_argument("--page-size", type=int, default=api_client.DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=api_client.DEFAULT_MAX_PAGES)
    parser.add_argument("--keep-backups", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _stream_args(args: argparse.Namespace, stream: str) -> argparse.Namespace:
    return argparse.Namespace(since=args.since, stream=stream, initial_days=args.initial_days)


def plan_org_targets(
    items: list[dict[str, Any]],
    index: dict[str, list[str]],
    mapping: dict[str, str],
    include_new: bool,
) -> dict[str, str | None]:
    targets: dict[str, str | None] = {}
    for item in items:
        api_id = normalize(item.get("id"))
        if not api_id:
            continue
        resolved, _ = resolve_org_id(item, index, mapping)
        if resolved:
            targets[api_id] = resolved
            continue
        if include_new:
            name = normalize(item.get("chinese_name")) or normalize(item.get("english_name"))
            targets[api_id] = stable_org_id(name) if name else None
            continue
        targets[api_id] = None
    return targets


def generated_org_row(org_id: str, name: str, english_name: str, now: str) -> dict[str, str]:
    return {
        "org_id": org_id,
        "chinese_name": name,
        "english_name": english_name,
        "last_updated": now,
        "org_verification_status": "unverified",
    }


def run(args: argparse.Namespace, client: Any | None = None) -> dict[str, Any]:
    if not args.investors.is_file():
        raise FileNotFoundError(f"investor snapshot not found: {args.investors}")
    if not args.organizations.is_file():
        raise FileNotFoundError(f"organization snapshot not found: {args.organizations}")
    state = load_state(args.state)
    streams = state.get("streams") or {}
    mapping: dict[str, str] = dict(state.get("org_mapping") or {})

    org_rows, org_fields = read_csv_file(args.organizations)
    investor_rows, investor_fields = read_csv_file(args.investors)
    org_index = build_org_index(org_rows)
    org_by_id = {normalize(row.get(ORG_ID)): row for row in org_rows}

    if client is None:
        api_client.load_repo_env(ROOT)
        client = api_client.InvestorApiClient()

    investor_since = resolve_since(_stream_args(args, "investors"), streams, investor_rows)
    org_since = resolve_since(_stream_args(args, "funds"), streams, org_rows)

    org_fetch = api_client.fetch_updated(
        client, ORG_ENDPOINT, org_since, include="research,investors_count",
        page_size=args.page_size, max_pages=args.max_pages,
    )
    investor_fetch = api_client.fetch_updated(
        client, INVESTOR_ENDPOINT, investor_since, include="research,compliance",
        page_size=args.page_size, max_pages=args.max_pages,
    )

    org_targets = plan_org_targets(org_fetch.items, org_index, mapping, args.include_new)
    for api_id, org_id in org_targets.items():
        if org_id:
            mapping[api_id] = org_id

    def org_new_identifier(item: dict[str, Any]) -> str | None:
        return org_targets.get(normalize(item.get("id")))

    def org_lookup_identifier(item: dict[str, Any]) -> str | None:
        return org_targets.get(normalize(item.get("id")))

    org_merge = merge_records(
        org_rows, org_fetch.items, ORG_ID, ORG_FIELDS, args.mode, args.include_new,
        new_identifier=org_new_identifier, lookup_identifier=org_lookup_identifier,
    )
    existing_org_ids = {normalize(row.get(ORG_ID)) for row in org_merge["rows"]}
    generated_orgs: list[dict[str, str]] = []
    now = api_client.to_utc_iso(datetime.now(timezone.utc))

    def investor_new_identifier(item: dict[str, Any]) -> str | None:
        return normalize(item.get("id"))

    def decorate_new_investor(row: dict[str, str], item: dict[str, Any]) -> bool:
        fund = item.get("fund") or {}
        resolved, _ = resolve_org_id(fund, org_index, mapping)
        if not resolved:
            name = normalize(fund.get("chinese_name")) or normalize(fund.get("english_name"))
            if not name:
                return False
            resolved = stable_org_id(name)
            if resolved not in existing_org_ids:
                generated_orgs.append(generated_org_row(resolved, name, normalize(fund.get("english_name")), now))
                existing_org_ids.add(resolved)
            mapping[normalize(fund.get("id"))] = resolved
        row[ORG_ID] = resolved
        row["org"] = normalize(fund.get("chinese_name"))
        row["org_name_matched"] = normalize(fund.get("chinese_name"))
        org = org_by_id.get(resolved) or next(
            (item_row for item_row in generated_orgs if item_row.get(ORG_ID) == resolved), {}
        )
        for field in ("org_sector_focus", "org_investment_stage", "org_headquarters"):
            if org.get(field):
                row[field] = org[field]
        return True

    investor_merge = merge_records(
        investor_rows, investor_fetch.items, INVESTOR_ID, INVESTOR_FIELDS, args.mode, args.include_new,
        new_identifier=investor_new_identifier, decorate_new=decorate_new_investor,
    )

    counts = {
        "investors": _counts_payload(investor_fetch, investor_merge),
        "organizations": _counts_payload(org_fetch, org_merge),
        "generated_organizations": len(generated_orgs),
    }
    changed_total = investor_merge["change_count"] + org_merge["change_count"] + len(generated_orgs)
    metadata_only = (
        investor_merge["counters"].get("timestamp_only", 0)
        + org_merge["counters"].get("timestamp_only", 0)
    )
    write_needed = changed_total > 0 or metadata_only > 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = None
    written: list[str] = []
    if write_needed and not args.dry_run:
        backup = create_backup([args.investors, args.organizations], args.backup_dir, run_id)
        all_org_rows = org_merge["rows"] + generated_orgs
        org_output_fields = org_fields or [ORG_ID, *(spec.column for spec in ORG_FIELDS), "last_updated"]
        investor_output_fields = investor_fields or [INVESTOR_ID, ORG_ID, *(spec.column for spec in INVESTOR_FIELDS), "last_updated"]
        write_csv_atomic(args.organizations, org_output_fields, all_org_rows)
        write_csv_atomic(args.investors, investor_output_fields, investor_merge["rows"])
        written = [str(args.investors.resolve()), str(args.organizations.resolve())]

    updated_streams = dict(streams)
    updated_streams["investors"] = _next_stream(investor_fetch, investor_since, now)
    updated_streams["funds"] = _next_stream(org_fetch, org_since, now)
    next_state = {
        "state_version": SYNC_STATE_VERSION,
        "updated_at": now,
        "streams": updated_streams,
        "org_mapping": {key: value for key, value in sorted(mapping.items()) if value},
        "last_report": None,
    }

    report = {
        "status": "ok",
        "run_id": run_id,
        "dry_run": args.dry_run,
        "mode": args.mode,
        "since": {"investors": api_client.to_utc_iso(investor_since), "funds": api_client.to_utc_iso(org_since)},
        "counts": counts,
        "changed_total": changed_total,
        "metadata_only_total": metadata_only,
        "changes": {"investors": investor_merge["changes"], "organizations": org_merge["changes"]},
        "conflicts": {"investors": investor_merge["conflicts"], "organizations": org_merge["conflicts"]},
        "unmatched_organizations": org_merge["unmatched"],
        "api": {
            "investors": _fetch_payload(investor_fetch),
            "funds": _fetch_payload(org_fetch),
        },
        "backup": backup,
        "written": written,
        "state": str(args.state.resolve()),
    }
    report_path = args.report_dir / f"{run_id}_report.json"
    if not args.dry_run:
        write_json_atomic(report_path, report)
        next_state["last_report"] = str(report_path.resolve())
        write_json_atomic(args.state, next_state)
        report["report_path"] = str(report_path.resolve())
        report["pruned_backups"] = prune_backups(args.backup_dir, args.keep_backups)
    return report


def _next_stream(fetch: api_client.FetchResult, since: datetime, now: str) -> dict[str, Any]:
    if fetch.stop_reason == "max_pages" and fetch.oldest_seen:
        next_since = (api_client.parse_datetime(fetch.oldest_seen) or since) - timedelta(seconds=1)
        complete = False
    else:
        next_since = api_client.parse_datetime(fetch.newest_seen) if fetch.newest_seen else since
        complete = True
    return {
        "last_sync": api_client.to_utc_iso(next_since),
        "last_run": now,
        "complete": complete,
        "fetch": _fetch_payload(fetch),
    }


def _fetch_payload(fetch: api_client.FetchResult) -> dict[str, Any]:
    return {
        "pages": fetch.pages,
        "total": fetch.total,
        "fetched": fetch.fetched,
        "included": fetch.unique,
        "duplicates": max(0, fetch.duplicates),
        "skipped_without_timestamp": fetch.skipped_without_timestamp,
        "newest_seen": fetch.newest_seen,
        "oldest_seen": fetch.oldest_seen,
        "stop_reason": fetch.stop_reason,
    }


def _counts_payload(fetch: api_client.FetchResult, merge: dict[str, Any]) -> dict[str, Any]:
    return {
        **_fetch_payload(fetch),
        **merge["counters"],
        "changed": merge["change_count"],
        "conflicts": merge["conflict_count"],
    }


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.page_size < 1 or args.page_size > 500:
        print(json.dumps({"status": "error", "error": "--page-size must be 1..500"}, ensure_ascii=False))
        return EXIT_USAGE
    if args.max_pages < 1:
        print(json.dumps({"status": "error", "error": "--max-pages must be >= 1"}, ensure_ascii=False))
        return EXIT_USAGE
    try:
        report = run(args)
    except api_client.ApiError as error:
        print(json.dumps({"status": "error", "stage": "api", "code": error.code, "error": str(error)}, ensure_ascii=False))
        return EXIT_API
    except (ValueError, FileNotFoundError) as error:
        print(json.dumps({"status": "error", "stage": "data", "error": str(error)}, ensure_ascii=False))
        return EXIT_DATA
    except (OSError, csv.Error) as error:
        print(json.dumps({"status": "error", "stage": "io", "error": str(error)}, ensure_ascii=False))
        return EXIT_IO
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
