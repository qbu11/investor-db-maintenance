#!/usr/bin/env python3
"""Recall evaluation for the v2 tag+term+lexical investor search.

Metrics are split into system behaviour (parse, diagnostics, groups) and
evidence proxies (how many returned rows actually carry the expected tag or
term). The evidence proxies are not human-judged relevance; use the label
template + ``--labels`` for precision/MRR.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEARCH_PATH = ROOT / "scripts" / "search_investors_v2.py"
DEFAULT_RELEASE_DIR = ROOT / "output_v2"
DEFAULT_QUERIES = ROOT / "doc" / "investor_search_acceptance_queries_v3.csv"
QUERY_FIELDS = ["case_id", "category", "query", "sector", "expected_tags",
                "expected_terms", "expected_behavior", "negative_tag", "notes"]
def discover_investors_file(release_dir: Path) -> Path:
    manifests = sorted(release_dir.glob("investor_search_*_manifest.json"))
    if manifests:
        manifest = json.loads(max(manifests, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8"))
        for name in manifest.get("outputs", {}):
            if name.startswith("investors_search_"):
                return release_dir / name
    candidates = sorted(release_dir.glob("investors_search_*.csv"))
    if not candidates:
        raise SystemExit(f"no investors_search file found in {release_dir}")
    return candidates[0]


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def split_field(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    search = load_module("investor_search_v2_eval", SEARCH_PATH)
    index = search.SearchIndex(args.release_dir, load_evidence=False)
    cases = read_csv(args.queries)
    if cases:
        missing = [field for field in QUERY_FIELDS if field not in cases[0]]
        if missing:
            raise SystemExit(f"query file is missing columns: {', '.join(missing)}")

    row_meta: dict[str, tuple[set[str], set[str]]] = {}
    for row in read_csv(discover_investors_file(args.release_dir)):
        row_meta[row["source_user_id"]] = (
            set(json.loads(row.get("all_tag_names") or "[]")),
            set(json.loads(row.get("term_names") or "[]")),
        )

    # A parent-tag query may legitimately return rows that only carry a child
    # tag (spec 8.2). Count those as evidence hits.
    tag_names = {tag_id: row["canonical_name"] for tag_id, row in index.tags.by_id.items()}
    descendant_names: dict[str, set[str]] = {
        tag_names[tag_id]: {tag_names[child] for child in index.tags.descendants(tag_id)}
        for tag_id in index.tags.by_id
    }
    row_tag_names = [
        set(json.loads(row.get("all_tag_names") or "[]"))
        for row in index.investors
    ]
    tag_population: dict[str, int] = {}
    for tag_id, name in tag_names.items():
        related_names = {name} | descendant_names.get(name, set())
        tag_population[name] = sum(1 for names in row_tag_names if related_names & names)

    details: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        record: dict[str, Any] = {
            "case_id": case["case_id"], "category": case["category"], "query": case["query"],
            "expected_tags": split_field(case.get("expected_tags") or ""),
            "expected_terms": split_field(case.get("expected_terms") or ""),
            "expected_behavior": case.get("expected_behavior") or "",
            "negative_tag": case.get("negative_tag") or "",
        }
        try:
            result = index.search(
                query=case["query"], sector=case.get("sector") or "",
                top_k=args.top_k, max_per_org=args.max_per_org,
            )
        except Exception as error:  # noqa: BLE001 - report per-case failures, keep going
            record["error"] = f"{type(error).__name__}: {error}"
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            details.append(record)
            continue
        record["error"] = None
        record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)

        interpretation = result["query_interpretation"]
        items = result["items"]
        parsed_tags = {item["canonical_name"] for item in interpretation["parsed_tags"]}
        parsed_terms = {item["term"] for item in interpretation["parsed_terms"]}
        expected_tags = set(record["expected_tags"])
        expected_terms = set(record["expected_terms"])
        item_ids = [item["source_user_id"] for item in items]
        top_rows = item_ids[: args.top_k]

        expected_tag_set = set(expected_tags)
        for tag_name in expected_tags:
            expected_tag_set |= descendant_names.get(tag_name, set())
        tag_hits = sum(
            1 for item_id in top_rows
            if expected_tags and expected_tag_set & row_meta.get(item_id, (set(), set()))[0]
        )
        term_hits = sum(
            1 for item_id in top_rows
            if expected_terms and expected_terms & row_meta.get(item_id, (set(), set()))[1]
        )
        fallback_rows = sum(1 for item in items if item.get("fallback"))
        group_counts: dict[str, int] = {}
        for item in items:
            group_counts[item["match_group"]] = group_counts.get(item["match_group"], 0) + 1

        behavior = record["expected_behavior"]
        is_broad = behavior == "broad"
        is_unmapped = behavior == "unmapped"
        diagnostic_ok = None
        if is_broad:
            diagnostic_ok = bool(interpretation["broad_terms"]) and not items
        elif is_unmapped:
            diagnostic_ok = bool(interpretation["unmapped_terms"]) and not items

        record.update({
            "parsed_tags": sorted(parsed_tags),
            "parsed_terms": sorted(parsed_terms),
            "broad_terms": interpretation["broad_terms"],
            "unmapped_terms": interpretation["unmapped_terms"],
            "residual_terms": interpretation["residual_terms"],
            "returned": len(items),
            "zero_result": len(items) == 0,
            "tag_resolve_ok": bool(expected_tags) and expected_tags.issubset(parsed_tags),
            "term_resolve_ok": bool(expected_terms) and expected_terms.issubset(parsed_terms),
            "diagnostic_ok": diagnostic_ok,
            "negative_in_parsed": bool(record["negative_tag"]) and record["negative_tag"] in parsed_tags,
            "tag_hit_at_k": round(tag_hits / len(top_rows), 3) if top_rows and expected_tags else None,
            "expected_tag_population": max(
                (tag_population.get(name, 0) for name in expected_tags), default=0
            ) if expected_tags else None,
            "term_hit_at_k": round(term_hits / len(top_rows), 3) if top_rows and expected_terms else None,
            "fallback_rate": round(fallback_rows / len(items), 3) if items else None,
            "groups": group_counts,
            "term_evidence": result["search_meta"].get("term_evidence"),
            "no_match_reason": result["search_meta"].get("no_match_reason"),
            "item_ids": item_ids,
            "top_items": [
                {
                    "rank": rank, "source_user_id": item["source_user_id"], "name": item.get("name"),
                    "organization": item.get("organization"), "score": item.get("score"),
                    "match_group": item["match_group"], "fallback": item.get("fallback"),
                    "matched_tags": item.get("matched_tag_names"),
                    "matched_terms": item.get("matched_term_names"),
                }
                for rank, item in enumerate(items[: args.label_limit], 1)
            ],
        })
        details.append(record)

    pairs: list[dict[str, Any]] = []
    by_case = {detail["case_id"]: detail for detail in details}
    for detail in details:
        behavior = detail.get("expected_behavior") or ""
        if behavior.startswith("pair:"):
            target = behavior.split(":", 1)[1]
            if target in by_case:
                left = set(detail.get("item_ids") or [])
                right = set(by_case[target].get("item_ids") or [])
                union = left | right
                pairs.append({
                    "left": detail["case_id"], "right": target,
                    "jaccard": round(len(left & right) / len(union), 4) if union else 1.0,
                })

    def summarize(subset: list[dict[str, Any]]) -> dict[str, Any]:
        ran = [item for item in subset if item.get("error") is None]
        resolve_cases = [item for item in ran if item.get("expected_tags")]
        term_cases = [item for item in ran if item.get("expected_terms")]
        diagnostic_cases = [item for item in ran if item.get("diagnostic_ok") is not None]
        regular = [item for item in ran if item.get("expected_behavior") not in ("broad", "unmapped")]

        def rate(values: list[bool]) -> float | None:
            return round(sum(1 for value in values if value) / len(values), 3) if values else None

        tag_hit_rates = [item["tag_hit_at_k"] for item in ran if item.get("tag_hit_at_k") is not None]
        term_hit_rates = [item["term_hit_at_k"] for item in ran if item.get("term_hit_at_k") is not None]
        exact_ratios = [
            item["groups"].get("exact", 0) / max(1, sum(item["groups"].values()))
            for item in ran if item.get("groups")
        ]
        return {
            "cases": len(subset),
            "errors": len(subset) - len(ran),
            "tag_resolve_rate": rate([item.get("tag_resolve_ok") for item in resolve_cases]),
            "term_resolve_rate": rate([item.get("term_resolve_ok") for item in term_cases]),
            "diagnostic_rate": rate([item.get("diagnostic_ok") for item in diagnostic_cases]),
            "zero_result_rate": rate([item.get("zero_result") for item in regular]),
            "avg_tag_hit_at_k": round(statistics.mean(tag_hit_rates), 3) if tag_hit_rates else None,
            "avg_term_hit_at_k": round(statistics.mean(term_hit_rates), 3) if term_hit_rates else None,
            "avg_exact_ratio": round(statistics.mean(exact_ratios), 3) if exact_ratios else None,
            "avg_fallback_rate": round(
                statistics.mean([item["fallback_rate"] for item in ran if item.get("fallback_rate") is not None]), 3
            ) if any(item.get("fallback_rate") is not None for item in ran) else None,
        }

    categories: dict[str, dict[str, Any]] = {}
    for category in sorted({detail["category"] for detail in details}):
        categories[category] = summarize([detail for detail in details if detail["category"] == category])

    latencies = sorted(item["latency_ms"] for item in details if "latency_ms" in item)
    return {
        "schema_version": "2.0",
        "release_dir": str(args.release_dir),
        "queries": str(args.queries),
        "top_k": args.top_k,
        "summary": summarize(details),
        "by_category": categories,
        "alias_pairs": pairs,
        "boundary": {
            "negative_in_parsed": [d["case_id"] for d in details if d.get("negative_in_parsed")],
        },
        "latency_ms": {
            "p50": round(statistics.median(latencies), 2) if latencies else None,
            "p95": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95) - 1)], 2) if latencies else None,
        },
        "cases": details,
    }


def dump_label_template(report: dict[str, Any], path: Path) -> None:
    fields = ["case_id", "query", "rank", "source_user_id", "name", "organization",
              "score", "match_group", "matched_tags", "matched_terms", "label"]
    rows: list[dict[str, Any]] = []
    for detail in report["cases"]:
        for item in detail.get("top_items") or []:
            rows.append({
                "case_id": detail["case_id"], "query": detail["query"], "rank": item["rank"],
                "source_user_id": item["source_user_id"], "name": item.get("name") or "",
                "organization": item.get("organization") or "", "score": item.get("score"),
                "match_group": item.get("match_group") or "",
                "matched_tags": ";".join(item.get("matched_tags") or []),
                "matched_terms": ";".join(item.get("matched_terms") or []),
                "label": "",
            })
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"label template written: {path} ({len(rows)} rows)", file=sys.stderr)


def score_labels(report: dict[str, Any], label_path: Path) -> dict[str, Any]:
    labels = read_csv(label_path)
    by_case: dict[str, dict[str, int]] = {}
    for row in labels:
        value = str(row.get("label") or "").strip()
        if value in ("0", "1"):
            by_case.setdefault(row["case_id"], {})[row["source_user_id"]] = int(value)
    precisions: list[float] = []
    reciprocal_ranks: list[float] = []
    for detail in report["cases"]:
        case_labels = by_case.get(detail["case_id"])
        if not case_labels:
            continue
        item_ids = detail.get("item_ids") or []
        judged10 = [case_labels[item] for item in item_ids[:10] if item in case_labels]
        if judged10:
            precisions.append(sum(judged10) / len(judged10))
        first_positive = next((rank for rank, item in enumerate(item_ids, 1) if case_labels.get(item) == 1), None)
        if first_positive:
            reciprocal_ranks.append(1.0 / first_positive)
    return {
        "labeled_cases": len(by_case),
        "precision_at_10": round(statistics.mean(precisions), 4) if precisions else None,
        "mrr": round(statistics.mean(reciprocal_ranks), 4) if reciprocal_ranks else None,
    }


def print_summary(report: dict[str, Any]) -> None:
    print("== overall ==")
    for key, value in report["summary"].items():
        print(f"  {key}: {value}")
    print("== by category ==")
    header = ["category", "cases", "tag_resolve_rate", "term_resolve_rate", "diagnostic_rate",
              "zero_result_rate", "avg_tag_hit_at_k", "avg_term_hit_at_k", "avg_exact_ratio", "avg_fallback_rate"]
    print("  " + " | ".join(header))
    for name, stats in report["by_category"].items():
        row = {"category": name, **stats}
        print("  " + " | ".join(str(row.get(key)) for key in header))
    print("== alias pairs ==")
    for pair in report["alias_pairs"]:
        print(f"  {pair['left']} vs {pair['right']}: jaccard={pair['jaccard']}")
    print("== boundary ==")
    print("  negative_in_parsed:", report["boundary"]["negative_in_parsed"] or "none")
    print("== latency ms ==", report["latency_ms"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recall evaluation for v2 investor search")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--release-dir", type=Path, default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--max-per-org", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dump-labels", type=Path)
    parser.add_argument("--label-limit", type=int, default=20)
    parser.add_argument("--labels", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate(args)
    if args.dump_labels:
        dump_label_template(report, args.dump_labels)
    if args.labels:
        report["human_labels"] = score_labels(report, args.labels)
    print_summary(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"full report written: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
