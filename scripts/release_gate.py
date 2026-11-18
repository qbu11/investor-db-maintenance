#!/usr/bin/env python3
"""Evaluate a release report against the policy thresholds.

Modes:
- regression: candidate must not drop more than max_metric_drop against baseline.
- release: candidate must satisfy absolute targets (needs human labels).

Exit codes: 0 pass, 2 usage, 24 gate failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "doc" / "investor_search_release_policy_v3.json"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GATE = 24

SYSTEM_METRICS = {
    "tag_resolve_rate": "min",
    "term_resolve_rate": "min",
    "diagnostic_rate": "min",
    "zero_result_rate": "max",
    "avg_exact_ratio": "min",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_report(
    candidate: dict[str, Any],
    baseline: dict[str, Any] | None,
    policy: dict[str, Any],
    mode: str,
) -> list[dict[str, Any]]:
    thresholds = policy.get("thresholds") or {}
    summary = candidate.get("summary") or {}
    base_summary = (baseline or {}).get("summary") or {}
    violations: list[dict[str, Any]] = []

    if (summary.get("errors") or 0) > thresholds.get("errors_max", 0):
        violations.append({"metric": "errors", "value": summary.get("errors"), "rule": "errors must be 0"})

    for metric, direction in SYSTEM_METRICS.items():
        value = summary.get(metric)
        if value is None:
            continue
        if mode == "release":
            key = f"{metric}_{'min' if direction == 'min' else 'max'}"
            target = thresholds.get(key)
            if target is not None:
                if direction == "min" and value < target:
                    violations.append({"metric": metric, "value": value, "rule": f">= {target}"})
                if direction == "max" and value > target:
                    violations.append({"metric": metric, "value": value, "rule": f"<= {target}"})
        else:
            base = base_summary.get(metric)
            if base is not None:
                drop = (base - value) if direction == "min" else (value - base)
                if drop > thresholds.get("max_metric_drop", 0.02):
                    violations.append({
                        "metric": metric, "value": value, "baseline": base,
                        "rule": f"regression beyond {thresholds.get('max_metric_drop')}",
                    })

    v2_fine = (candidate.get("by_category") or {}).get("v2_fine") or {}
    fallback = v2_fine.get("avg_fallback_rate")
    if mode == "release" and fallback is not None:
        target = thresholds.get("v2_fine_fallback_rate_max")
        if target is not None and fallback > target:
            violations.append({"metric": "v2_fine.avg_fallback_rate", "value": fallback, "rule": f"<= {target}"})

    labels = candidate.get("human_labels") or {}
    if mode == "release":
        labeled = labels.get("labeled_cases") or 0
        if labeled < thresholds.get("labeled_cases_min", 0):
            violations.append({
                "metric": "human_labels.labeled_cases", "value": labeled,
                "rule": f">= {thresholds.get('labeled_cases_min')}",
            })
        precision = labels.get("precision_at_10")
        if precision is not None and precision < thresholds.get("human_precision_at_10_min", 0):
            violations.append({"metric": "human_labels.precision_at_10", "value": precision,
                               "rule": f">= {thresholds.get('human_precision_at_10_min')}"})
        mrr = labels.get("mrr")
        if mrr is not None and mrr < thresholds.get("human_mrr_min", 0):
            violations.append({"metric": "human_labels.mrr", "value": mrr,
                               "rule": f">= {thresholds.get('human_mrr_min')}"})
    elif labels:
        for metric in ("precision_at_10", "mrr"):
            value = labels.get(metric)
            base = (baseline or {}).get("human_labels", {}).get(metric) if baseline else None
            if value is not None and base is not None and base - value > thresholds.get("max_metric_drop", 0.02):
                violations.append({"metric": f"human_labels.{metric}", "value": value, "baseline": base,
                                   "rule": "regression"})
    return violations


def run(policy_path: Path, candidate_path: Path, baseline_path: Path | None, mode: str) -> dict[str, Any]:
    policy = load_json(policy_path)
    candidate = load_json(candidate_path)
    baseline = load_json(baseline_path) if baseline_path and baseline_path.is_file() else None
    violations = check_report(candidate, baseline, policy, mode)
    return {
        "policy_version": policy.get("policy_version"),
        "mode": mode,
        "status": "pass" if not violations else "fail",
        "violations": violations,
        "candidate": str(candidate_path),
        "baseline": str(baseline_path) if baseline_path else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Release gate for investor search")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--mode", choices=["regression", "release"], default="regression")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run(args.policy, args.report, args.baseline, args.mode)
    except (OSError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return EXIT_USAGE
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["status"] == "pass" else EXIT_GATE


if __name__ == "__main__":
    raise SystemExit(main())
