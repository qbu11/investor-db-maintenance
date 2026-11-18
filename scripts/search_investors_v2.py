#!/usr/bin/env python3
"""Deterministic v2 investor search: controlled tags + retrieval terms + text.

Query understanding:
- longest-match over tag canonical names/aliases and scenario terms;
- residual segments become diagnostics, never invented concepts;
- a query with no tag and no term returns an explicit ``unmapped_query``
  instead of pretending text noise is a real match.

Ranking (highest first):
- exact tag hit (own) 50 / inherited 42 / scenario term 45;
- child tag under a queried parent 35 / related tag 20;
- parent-tag fallback for a narrower query 12 (explicitly flagged);
- text evidence is IDF-weighted and only breaks ties inside a group.

Every item carries ``match_group`` (exact/related/lexical), matched tags and
terms, and ``fallback`` so callers can separate verified from expanded rows.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELEASE_DIR = ROOT / "output_v2"
RELEASE_ID = "v2_20261118"
SCHEMA_VERSION = "2.0"

OUTPUT_NAMES = {
    "tags": f"tags_{RELEASE_ID}.csv",
    "terms": f"terms_{RELEASE_ID}.csv",
    "relations": f"tag_relations_{RELEASE_ID}.csv",
    "investors": f"investors_search_{RELEASE_ID}.csv",
    "organizations": f"organizations_search_{RELEASE_ID}.csv",
    "manifest": f"investor_search_{RELEASE_ID}_manifest.json",
}

BROAD_TERMS = frozenset({
    "科技", "科技创新", "高科技", "tmt", "新一代信息技术", "战略性新兴产业",
    "未来产业", "新经济", "互联网", "数字经济", "信息技术",
})
STOP_RESIDUALS = frozenset({"的", "和", "与", "及", "以及", "公司", "企业", "行业", "领域", "方向"})
CJK = re.compile(r"[㐀-鿿]")
LATIN_WORD = re.compile(r"[a-z0-9][a-z0-9.+#-]*")
CJK_SEGMENT = re.compile(r"[㐀-鿿]+")


class SearchInputError(ValueError):
    pass


def normalized(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split()).casefold()


def contains_key(text_key: str, key: str) -> bool:
    if not key or not text_key:
        return False
    if CJK.search(key):
        return key in text_key
    if len(key) < 2:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text_key) is not None


def tokenize(value: Any) -> set[str]:
    text = normalized(value)
    tokens: set[str] = set(LATIN_WORD.findall(text))
    for segment in CJK_SEGMENT.findall(text):
        for size in (2, 3):
            tokens.update(segment[index:index + size] for index in range(max(0, len(segment) - size + 1)))
    return tokens


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def discover_artifacts(release_dir: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    """Find the manifest and artifacts in either the v2 or v3 layout."""
    manifests = sorted(release_dir.glob("investor_search_*_manifest.json"))
    if not manifests:
        raise SearchInputError(f"no release manifest found in {release_dir}")
    manifest_path = max(manifests, key=lambda path: path.stat().st_mtime)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    outputs = manifest.get("outputs") or {}
    if not outputs:
        raise SearchInputError(f"manifest has no outputs: {manifest_path}")
    prefixes = {
        "tags": "tags_",
        "terms": "terms_",
        "relations": "tag_relations_",
        "evidence": "entity_tag_evidence_",
        "investors": "investors_search_",
        "organizations": "organizations_search_",
    }
    artifacts: dict[str, Path] = {}
    for key, prefix in prefixes.items():
        match = next((name for name in outputs if name.startswith(prefix)), None)
        if match is None:
            raise SearchInputError(f"manifest is missing {key} output")
        artifacts[key] = release_dir / match
    return manifest, artifacts




def parse_json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def parse_amount(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def ranges_overlap(inv_min: int | None, inv_max: int | None, ask_min: int, ask_max: int) -> bool:
    if inv_min is None or inv_max is None:
        return False
    return inv_min <= ask_max and inv_max >= ask_min


def currency_matches(candidate: str, requested: str) -> bool:
    if not requested:
        return True
    return normalized(requested) in normalized(candidate)


class TagMetadata:
    def __init__(self, tag_rows: list[dict[str, str]]) -> None:
        self.by_id = {row["tag_id"]: row for row in tag_rows}
        self.name = {row["tag_id"]: row["canonical_name"] for row in tag_rows}
        self.facet = {row["tag_id"]: row["facet"] for row in tag_rows}
        self.parent: dict[str, str] = {row["tag_id"]: row["parent_id"] for row in tag_rows if row.get("parent_id")}
        self.children: dict[str, list[str]] = defaultdict(list)
        for child, parent in self.parent.items():
            self.children[parent].append(child)
        self.alias_to_tag: dict[str, tuple[str, str]] = {}
        for row in tag_rows:
            for alias in [row["canonical_name"], *[a for a in (row.get("aliases") or "").split(";") if a.strip()]]:
                self.alias_to_tag[normalized(alias)] = (row["tag_id"], alias.strip())
        self.alias_keys_by_length = sorted(self.alias_to_tag, key=len, reverse=True)

    def ancestors(self, tag_id: str) -> list[str]:
        chain: list[str] = []
        current = self.parent.get(tag_id)
        while current and current not in chain:
            chain.append(current)
            current = self.parent.get(current)
        return chain

    def descendants(self, tag_id: str) -> set[str]:
        result: set[str] = set()
        stack = list(self.children.get(tag_id, []))
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(self.children.get(current, []))
        return result


class TermMetadata:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = {row["term_id"]: row for row in rows}
        self.name = {row["term_id"]: row["term"] for row in rows}
        self.tag_ids = {
            row["term_id"]: [value for value in row["tag_ids"].split(";") if value]
            for row in rows
        }
        self.key_to_term: dict[str, str] = {}
        for row in rows:
            for key in [row["term"], *[s for s in (row.get("synonyms") or "").split(",") if s.strip()]]:
                self.key_to_term[normalized(key)] = row["term_id"]
        self.keys_by_length = sorted(self.key_to_term, key=len, reverse=True)


class QueryPlan:
    def __init__(self) -> None:
        self.tags: list[dict[str, Any]] = []
        self.terms: list[dict[str, Any]] = []
        self.broad_terms: list[str] = []
        self.unmapped_terms: list[str] = []
        self.residual_terms: list[str] = []


def parse_query(
    query: str, tag_meta: TagMetadata, term_meta: TermMetadata, sector: str = ""
) -> QueryPlan:
    plan = QueryPlan()
    text = normalized(query)
    raw_matches: list[tuple[int, int, str, str]] = []  # start, end, kind, id

    for key in sorted({*tag_meta.alias_keys_by_length, *term_meta.keys_by_length}, key=len, reverse=True):
        if len(key) < 2:
            continue
        start = text.find(key)
        while start >= 0:
            if key in tag_meta.alias_to_tag:
                raw_matches.append((start, start + len(key), "tag", tag_meta.alias_to_tag[key][0]))
            elif key in term_meta.key_to_term:
                raw_matches.append((start, start + len(key), "term", term_meta.key_to_term[key]))
            start = text.find(key, start + 1)

    # Greedy left-to-right longest match. Sorting by start first makes the
    # choice deterministic when two candidates have the same length.
    raw_matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2], item[3]))
    selected: list[tuple[int, int, str, str]] = []
    for match in raw_matches:
        start, end = match[0], match[1]
        if any(start < other_end and end > other_start for other_start, other_end, _, _ in selected):
            continue
        selected.append(match)
    selected.sort(key=lambda item: item[0])

    seen_tags: set[str] = set()
    seen_terms: set[str] = set()

    def add_tag(tag_id: str, via_term: bool = False) -> None:
        if tag_id in seen_tags:
            return
        seen_tags.add(tag_id)
        plan.tags.append({
            "tag_id": tag_id,
            "canonical_name": tag_meta.name[tag_id],
            "facet": tag_meta.facet[tag_id],
            "via_term": via_term,
        })

    for _, _, kind, identifier in selected:
        if kind == "tag":
            add_tag(identifier)
        elif identifier not in seen_terms:
            seen_terms.add(identifier)
            plan.terms.append({
                "term_id": identifier,
                "term": term_meta.name[identifier],
                "tag_ids": term_meta.tag_ids[identifier],
            })
            for tag_id in term_meta.tag_ids[identifier]:
                add_tag(tag_id, via_term=True)

    if sector:
        sector_key = normalized(sector)
        if sector_key not in tag_meta.alias_to_tag:
            plan.unmapped_terms.append(sector)
        else:
            sector_tag = tag_meta.alias_to_tag[sector_key][0]
            if sector_tag not in seen_tags:
                plan.tags.insert(0, {
                    "tag_id": sector_tag,
                    "canonical_name": tag_meta.name[sector_tag],
                    "facet": tag_meta.facet[sector_tag],
                })

    cursor = 0
    residuals: list[str] = []
    for start, end in sorted((match[0], match[1]) for match in selected):
        if start > cursor:
            residuals.append(text[cursor:start].strip())
        cursor = max(cursor, end)
    if cursor < len(text):
        residuals.append(text[cursor:].strip())
    plan.residual_terms = [item for item in residuals if item and item not in STOP_RESIDUALS]
    for residual in plan.residual_terms:
        if len(residual) < 2:
            continue
        if residual in BROAD_TERMS:
            plan.broad_terms.append(residual)
        else:
            plan.unmapped_terms.append(residual)
    return plan


class SearchIndex:
    def __init__(self, release_dir: Path, load_evidence: bool = True) -> None:
        self.release_dir = release_dir
        self.manifest, self.artifacts = discover_artifacts(release_dir)
        self.load_evidence = load_evidence
        self.tags = TagMetadata(read_csv(self.artifacts["tags"]))
        self.terms = TermMetadata(read_csv(self.artifacts["terms"]))
        self.relations = read_csv(self.artifacts["relations"])
        self.evidence_by_tag: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
        self.evidence_by_term: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
        if load_evidence:
            for row in read_csv(self.artifacts["evidence"]):
                if str(row.get("negative") or "").lower() == "true":
                    continue
                if row.get("tag_id"):
                    self.evidence_by_tag[(row["entity_type"], row["entity_id"], row["tag_id"])].append(row)
                if row.get("term_id"):
                    self.evidence_by_term[(row["entity_type"], row["entity_id"], row["term_id"])].append(row)
        self.investors: list[dict[str, Any]] = []
        for row in read_csv(self.artifacts["investors"]):
            row["own_tags"] = set(parse_json_list(row.get("own_tag_ids")))
            row["inherited_tags"] = set(parse_json_list(row.get("inherited_tag_ids")))
            row["all_tags"] = set(parse_json_list(row.get("all_tag_ids")))
            row["terms"] = set(parse_json_list(row.get("term_ids")))
            row["sector_tokens"] = tokenize(row.get("sector_search_text", ""))
            row["profile_tokens"] = tokenize(row.get("profile_search_text", ""))
            self.investors.append(row)
        self.by_id = {row["source_user_id"]: row for row in self.investors}
        self.related_tags: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        for row in self.relations:
            if row["relation_type"] in ("related", "is_a"):
                self.related_tags[row["source_tag_id"]].append(
                    (row["target_tag_id"], row["relation_type"], float(row["weight"]))
                )
        self.idf: dict[str, float] = {}
        document_count = len(self.investors) or 1
        df: Counter[str] = Counter()
        for row in self.investors:
            df.update(row["sector_tokens"] | row["profile_tokens"])
        for token, count in df.items():
            self.idf[token] = math.log(1 + document_count / (1 + count))

    def _text_score(self, query_tokens: set[str], row: dict[str, Any], weight: float) -> float:
        if not query_tokens:
            return 0.0
        total = sum(self.idf.get(token, 1.0) for token in query_tokens)
        if total <= 0:
            return 0.0
        hit = sum(
            self.idf.get(token, 1.0) for token in query_tokens
            if token in row["sector_tokens"] or token in row["profile_tokens"]
        )
        return (hit / total) * weight

    def search(
        self,
        query: str,
        sector: str = "",
        stage: str = "",
        currency: str = "",
        min_amount: int | None = None,
        max_amount: int | None = None,
        compliance: list[str] | None = None,
        top_k: int = 20,
        max_per_org: int = 3,
        include_risk: bool = False,
        require_known_amount: bool = False,
    ) -> dict[str, Any]:
        if top_k < 1 or top_k > 100:
            raise SearchInputError("top_k must be between 1 and 100")
        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            raise SearchInputError("min_amount cannot be greater than max_amount")
        plan = parse_query(query, self.tags, self.terms, sector)
        if not plan.tags and not plan.terms:
            return {
                "schema_version": SCHEMA_VERSION,
                "query_interpretation": self._interpretation(query, sector, plan),
                "search_meta": {
                    "retrieval_mode": "tag+term+lexical",
                    "groups": {"exact": 0, "related": 0, "lexical": 0},
                    "filtered": {},
                    "stable_sort": "group,score_desc,id_asc",
                    "no_match_reason": "unmapped_query",
                },
                "items": [],
            }

        stage_tag = None
        if stage:
            stage_key = normalized(stage)
            if stage_key in self.tags.alias_to_tag and self.tags.facet[self.tags.alias_to_tag[stage_key][0]] == "stage":
                stage_tag = self.tags.alias_to_tag[stage_key][0]
            else:
                raise SearchInputError(f"unknown stage: {stage}")

        allowed_compliance = {normalized(value) for value in compliance or []}
        weak_tags = {item["tag_id"] for item in plan.tags if item.get("via_term")}
        # Tokenize the canonical interpretation, not the raw query: alias pairs
        # such as AI / 人工智能 and 半导体 / 集成电路 must score identically.
        canonical_text = " ".join([
            *[item["canonical_name"] for item in plan.tags],
            *[item["term"] for item in plan.terms],
            *plan.residual_terms,
        ])
        query_tokens = tokenize(canonical_text)
        parsed_tag_ids = [item["tag_id"] for item in plan.tags]
        parsed_term_ids = [item["term_id"] for item in plan.terms]
        descendant_sets = {tag_id: self.tags.descendants(tag_id) for tag_id in parsed_tag_ids}
        ancestor_sets = {tag_id: self.tags.ancestors(tag_id) for tag_id in parsed_tag_ids}
        related_map: dict[str, float] = {}
        for tag_id in parsed_tag_ids:
            for target, relation_type, weight in self.related_tags.get(tag_id, []):
                related_map[target] = max(weight, related_map.get(target, 0.0))

        filtered: Counter[str] = Counter()
        candidates: list[dict[str, Any]] = []
        for row in self.investors:
            row_compliance = normalized(row.get("compliance_overall"))
            if not include_risk and row_compliance == "risk":
                filtered["risk"] += 1
                continue
            if allowed_compliance and row_compliance not in allowed_compliance:
                filtered["compliance"] += 1
                continue
            if currency and not currency_matches(row.get("currency", ""), currency):
                filtered["currency"] += 1
                continue
            inv_min = parse_amount(row.get("inv_min"))
            inv_max = parse_amount(row.get("inv_max"))
            amount_requested = min_amount is not None or max_amount is not None
            amount_status = "not_requested"
            if amount_requested:
                ask_min = min_amount if min_amount is not None else 0
                ask_max = max_amount if max_amount is not None else 10**9
                if inv_min is None or inv_max is None:
                    amount_status = "unknown"
                    if require_known_amount:
                        filtered["amount_unknown"] += 1
                        continue
                elif not ranges_overlap(inv_min, inv_max, ask_min, ask_max):
                    filtered["amount_mismatch"] += 1
                    continue
                else:
                    amount_status = "overlap"

            score = 0.0
            reasons: list[dict[str, Any]] = []
            matched_tags: list[str] = []
            matched_terms: list[str] = []
            fallback = False
            group = "lexical"

            for tag_id in parsed_tag_ids:
                weak = tag_id in weak_tags
                # Tags reached only through a scenario term are a supporting
                # signal, never equal to the term itself or a direct tag hit.
                own_score = 12.0 if weak else 50.0
                inherited_score = 10.0 if weak else 42.0
                child_score = 8.0 if weak else 35.0
                fallback_score = 5.0 if weak else 12.0
                if tag_id in row["own_tags"]:
                    score += own_score
                    matched_tags.append(tag_id)
                    reasons.append({"type": "tag_exact", "tag_id": tag_id, "score": own_score})
                    if not weak:
                        group = "exact"
                    elif group == "lexical":
                        group = "related"
                elif tag_id in row["inherited_tags"]:
                    score += inherited_score
                    matched_tags.append(tag_id)
                    reasons.append({"type": "tag_inherited", "tag_id": tag_id, "score": inherited_score})
                    if not weak:
                        group = "exact"
                    elif group == "lexical":
                        group = "related"
                else:
                    child_hits = descendant_sets[tag_id] & row["all_tags"]
                    if child_hits:
                        score += child_score
                        matched_tags.extend(sorted(child_hits))
                        reasons.append({
                            "type": "tag_child",
                            "queried": tag_id,
                            "children": sorted(self.tags.name[child] for child in child_hits),
                            "score": child_score,
                        })
                        if group == "lexical":
                            group = "related"
                    else:
                        ancestor_hits = [a for a in ancestor_sets[tag_id] if a in row["all_tags"]]
                        if ancestor_hits:
                            score += fallback_score
                            fallback = True
                            matched_tags.extend(ancestor_hits)
                            reasons.append({
                                "type": "tag_parent_fallback",
                                "queried": tag_id,
                                "ancestors": [self.tags.name[a] for a in ancestor_hits],
                                "score": fallback_score,
                            })
                            if group == "lexical":
                                group = "related"

            for term_id in parsed_term_ids:
                if term_id in row["terms"]:
                    score += 50.0
                    matched_terms.append(term_id)
                    reasons.append({"type": "term_exact", "term_id": term_id, "score": 50.0})
                    group = "exact"

            for related_tag, weight in related_map.items():
                if related_tag in row["all_tags"] and related_tag not in matched_tags:
                    component = weight * 20.0
                    score += component
                    matched_tags.append(related_tag)
                    reasons.append({"type": "tag_related", "tag_id": related_tag, "score": round(component, 2)})
                    if group == "lexical":
                        group = "related"

            text_score = self._text_score(query_tokens, row, 15.0)
            if text_score:
                score += text_score
                reasons.append({"type": "text_evidence", "score": round(text_score, 2)})

            if stage_tag:
                if stage_tag in row["all_tags"]:
                    score += 10.0
                    reasons.append({"type": "stage", "tag_id": stage_tag, "score": 10.0})
                else:
                    stage_text = normalized(row.get("stage_raw"))
                    if stage_text and normalized(stage) in stage_text:
                        score += 10.0
                        reasons.append({"type": "stage_text", "score": 10.0})

            if score <= 0:
                continue
            candidates.append({
                "source_user_id": row["source_user_id"],
                "name": row.get("name"),
                "organization": row.get("org_name_authoritative"),
                "org_id": row.get("org_id"),
                "score": round(score, 3),
                "match_group": group,
                "fallback": fallback,
                "match_reasons": reasons,
                "matched_tag_names": [self.tags.name[tag_id] for tag_id in matched_tags],
                "matched_term_names": [self.terms.name[term_id] for term_id in matched_terms],
                "matched_tag_ids": list(matched_tags),
                "matched_term_ids": list(matched_terms),
                "amount_status": amount_status,
                "data_confidence": row.get("person_data_confidence") or None,
                "verification_status": row.get("person_verification_status") or None,
                "compliance_overall": row.get("compliance_overall") or None,
            })

        group_rank = {"exact": 0, "related": 1, "lexical": 2}
        candidates.sort(key=lambda item: (group_rank[item["match_group"]], -item["score"], item["source_user_id"]))
        selected: list[dict[str, Any]] = []
        per_org: Counter[str] = Counter()
        group_counts: Counter[str] = Counter()
        for candidate in candidates:
            if per_org[candidate["org_id"]] >= max_per_org:
                continue
            per_org[candidate["org_id"]] += 1
            group_counts[candidate["match_group"]] += 1
            candidate["rank"] = len(selected) + 1
            selected.append(candidate)
            if len(selected) >= top_k:
                break

        if self.load_evidence:
            for item in selected:
                evidence, truncated = self._evidence_for(item)
                item["evidence"] = evidence
                item["evidence_truncated"] = truncated

        term_evidence: dict[str, int] = {}
        warnings: list[str] = []
        for term_id in parsed_term_ids:
            count = sum(1 for row in self.investors if term_id in row["terms"])
            term_evidence[self.terms.name[term_id]] = count
            if count == 0:
                warnings.append(f"场景词「{self.terms.name[term_id]}」在数据中无证据，结果仅为广义或文本候选")
            elif count < 5:
                warnings.append(f"场景词「{self.terms.name[term_id]}」仅 {count} 人命中，证据稀疏")
        exact_count = group_counts.get("exact", 0)
        if selected and exact_count == 0:
            warnings.append("无精确证据命中，结果为文本召回，需人工核验")
        elif selected and exact_count < max(1, len(selected) // 5):
            warnings.append(f"仅 {exact_count}/{len(selected)} 条为精确证据命中，其余为扩展或文本候选")
        return {
            "schema_version": SCHEMA_VERSION,
            "query_interpretation": self._interpretation(query, sector, plan),
            "search_meta": {
                "retrieval_mode": "tag+term+lexical",
                "groups": dict(sorted(group_counts.items())),
                "candidate_count": len(candidates),
                "term_evidence": term_evidence,
                "warnings": warnings,
                "filtered": dict(sorted(filtered.items())),
                "stable_sort": "group,score_desc,id_asc",
                "max_per_org": max_per_org,
                "no_match_reason": None if selected else "no_evidence_after_filters",
            },
            "items": selected,
        }

    def _evidence_for(self, item: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        rows: list[dict[str, str]] = []
        investor_id = item["source_user_id"]
        for tag_id in item.get("matched_tag_ids") or []:
            rows.extend(self.evidence_by_tag.get(("investor", investor_id, tag_id), []))
        for term_id in item.get("matched_term_ids") or []:
            rows.extend(self.evidence_by_term.get(("investor", investor_id, term_id), []))
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, str]] = []
        for row in rows:
            key = (row.get("evidence_text") or "", row.get("source_url") or "")
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        priority = {"explicit": 0, "derived": 1, "inferred": 2, "inherited": 3}
        unique.sort(key=lambda row: (
            priority.get(row.get("assignment_type") or "", 4),
            -float(row.get("confidence") or 0),
            -len(row.get("evidence_text") or ""),
        ))
        formatted = [{
            "tag_id": row.get("tag_id") or None,
            "tag_name": row.get("canonical_name"),
            "term_id": row.get("term_id") or None,
            "match_type": row.get("match_type"),
            "assignment_type": row.get("assignment_type"),
            "evidence_text": row.get("evidence_text"),
            "source_url": row.get("source_url"),
            "source_type": row.get("source_type"),
            "researched_at": row.get("researched_at"),
            "confidence": float(row.get("confidence") or 0),
            "batch_id": row.get("batch_id"),
        } for row in unique[:8]]
        return formatted, len(unique) > 8

    def _interpretation(self, query: str, sector: str, plan: QueryPlan) -> dict[str, Any]:
        return {
            "query": query,
            "sector": sector or None,
            "parsed_tags": plan.tags,
            "parsed_terms": plan.terms,
            "broad_terms": plan.broad_terms,
            "unmapped_terms": plan.unmapped_terms,
            "residual_terms": plan.residual_terms,
            "taxonomy_version": self.manifest.get("taxonomy_version"),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic v2 investor search")
    parser.add_argument("query")
    parser.add_argument("--release-dir", type=Path, default=None)
    parser.add_argument("--sector", default="")
    parser.add_argument("--stage", default="")
    parser.add_argument("--currency", default="")
    parser.add_argument("--min-amount", type=int)
    parser.add_argument("--max-amount", type=int)
    parser.add_argument("--compliance", action="append", default=[])
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--max-per-org", type=int, default=3)
    parser.add_argument("--include-risk", action="store_true")
    parser.add_argument("--require-known-amount", action="store_true")
    parser.add_argument("--debug-parse", action="store_true", help="print only the query interpretation")
    parser.add_argument("--no-evidence", action="store_true", help="skip loading and attaching evidence")
    return parser.parse_args()


def resolve_release_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import release_common  # type: ignore
        return release_common.resolve_release_dir()
    except Exception:  # noqa: BLE001 - fall back to the frozen v2 release
        return DEFAULT_RELEASE_DIR


def main() -> int:
    args = parse_args()
    release_dir = resolve_release_dir(args.release_dir)
    try:
        index = SearchIndex(release_dir, load_evidence=not args.no_evidence)
        if args.debug_parse:
            plan = parse_query(args.query, index.tags, index.terms, args.sector)
            print(json.dumps(index._interpretation(args.query, args.sector, plan), ensure_ascii=False, indent=2))
            return 0
        result = index.search(
            query=args.query, sector=args.sector, stage=args.stage,
            currency=args.currency, min_amount=args.min_amount, max_amount=args.max_amount,
            compliance=args.compliance, top_k=args.top_k, max_per_org=args.max_per_org,
            include_risk=args.include_risk, require_known_amount=args.require_known_amount,
        )
    except (OSError, csv.Error, json.JSONDecodeError, SearchInputError) as error:
        print(json.dumps({"error": "validation_error", "message": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
