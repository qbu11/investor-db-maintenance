#!/usr/bin/env python3
"""Build the v2 investor search release from the source snapshot and v2 taxonomy.

Design:
- Controlled tags: 150 tags from the v2 tag table (114 sector tags, 13 stage,
  9 business model, 10 capital type, 4 theme). Aliases are unique across tags.
- Retrieval term layer: application/scenario terms that do not fit the 150-tag
  budget (质检, 智能客服, 封测 ...), mapped to one or more controlled tags.
- Entity evidence: organization tags are the source of truth; investor tags are
  split into own (explicit/derived) and inherited tags.
- All inputs are frozen snapshots; output is deterministic (stable ordering,
  UTF-8-SIG, LF) and the manifest records SHA-256 hashes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
def _default_snapshot(stable_name: str, legacy_name: str) -> Path:
    stable = ROOT / "output" / stable_name
    return stable if stable.is_file() else ROOT / "output" / legacy_name


DEFAULT_INVESTORS = _default_snapshot("investors.csv", "investors_enriched_20260728_1745.csv")
DEFAULT_ORGANIZATIONS = _default_snapshot("organizations.csv", "organizations_enriched_20260728_1745.csv")
DEFAULT_TAGS = ROOT / "doc" / "投资人数据库规范标签表_v2.csv"
DEFAULT_TERMS = ROOT / "doc" / "investor_search_terms_v2.csv"
DEFAULT_OUTPUT_DIR = ROOT / "output_v2"
RELEASE_ID = "v2_20261118"
TAXONOMY_VERSION = "2.0.0"
SCHEMA_VERSION = "investor-search-csv/3.0"

def output_names(release_id: str) -> dict[str, str]:
    return {
        "tags": f"tags_{release_id}.csv",
        "terms": f"terms_{release_id}.csv",
        "relations": f"tag_relations_{release_id}.csv",
        "evidence": f"entity_tag_evidence_{release_id}.csv",
        "investors": f"investors_search_{release_id}.csv",
        "organizations": f"organizations_search_{release_id}.csv",
        "unmapped": f"unmapped_terms_{release_id}.csv",
        "manifest": f"investor_search_{release_id}_manifest.json",
    }


OUTPUT_NAMES = output_names(RELEASE_ID)

EVIDENCE_FIELDS = [
    "entity_type", "entity_id", "tag_id", "canonical_name", "facet",
    "assignment_type", "match_type", "source_field", "evidence_text",
    "confidence", "taxonomy_version",
    "term_id", "source_url", "source_type", "researched_at",
    "negative", "batch_id", "research_record_id", "evidence_id",
]
BASE_BATCH_ID = "base_20260728"
EVIDENCE_SCHEMA_VERSION = "3.0"

REQUIRED_INVESTOR_COLUMNS = {
    "source_user_id", "org_id", "name", "direction", "person_expertise",
    "person_investment_preferences", "person_background",
    "person_representative_deals", "org_sector_focus", "org_investment_stage",
    "currency", "inv_min", "inv_max", "compliance_overall",
}
REQUIRED_ORGANIZATION_COLUMNS = {
    "org_id", "chinese_name", "org_sector_focus", "org_investment_stage",
    "org_description", "org_notable_portfolio", "org_data_confidence",
    "org_verification_status",
}


def load_base_inputs(
    investors_path: Path, organizations_path: Path
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Load and validate the frozen base snapshot."""
    investors = read_csv(investors_path)
    organizations = read_csv(organizations_path)
    if not investors:
        raise SystemExit(f"investor snapshot is empty: {investors_path}")
    if not organizations:
        raise SystemExit(f"organization snapshot is empty: {organizations_path}")
    missing_investor = REQUIRED_INVESTOR_COLUMNS - set(investors[0])
    missing_organization = REQUIRED_ORGANIZATION_COLUMNS - set(organizations[0])
    if missing_investor:
        raise SystemExit(f"investor snapshot missing columns: {', '.join(sorted(missing_investor))}")
    if missing_organization:
        raise SystemExit(f"organization snapshot missing columns: {', '.join(sorted(missing_organization))}")
    investor_ids = [row["source_user_id"] for row in investors]
    org_ids = [row["org_id"] for row in organizations]
    if len(investor_ids) != len(set(investor_ids)):
        raise SystemExit("investor snapshot contains duplicate source_user_id")
    if len(org_ids) != len(set(org_ids)):
        raise SystemExit("organization snapshot contains duplicate org_id")
    known_orgs = set(org_ids)
    orphans = sorted({row["org_id"] for row in investors if row["org_id"] not in known_orgs})
    if orphans:
        raise SystemExit(f"investors reference unknown organizations: {', '.join(orphans[:5])}")
    return investors, organizations

PHASE_SPLIT = re.compile(r"[;；,，、|]+")
CONJUNCTION_SPLIT = re.compile(r"(?:以及|与|和|及|＋|\+)")
URL_PATTERN = re.compile(r"https?://\S+")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
LATIN_WORD = re.compile(r"[a-z0-9][a-z0-9.+#-]*")
CJK = re.compile(r"[㐀-鿿]")

SECTOR_FACETS = frozenset({"sector", "theme"})
STAGE_FACETS = frozenset({"stage"})
PERSON_FACETS = frozenset({"sector", "business_model", "theme"})


def normalized(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split()).casefold()


def clean_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "").strip()
    text = URL_PATTERN.sub(" ", text)
    text = EMAIL_PATTERN.sub(" ", text)
    text = PHONE_PATTERN.sub(" ", text)
    return " ".join(text.split())[:limit]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha1(value.encode('utf-8')).hexdigest()[:12]}"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    materialized = list(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)
    return len(materialized), len(fields)


def json_cell(values: Iterable[str]) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return json.dumps(unique, ensure_ascii=False)


def contains_key(text_key: str, key: str) -> bool:
    """Substring match for Chinese; word-boundary match for Latin keys."""
    if not key or not text_key:
        return False
    if CJK.search(key):
        return key in text_key
    if len(key) < 2:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text_key) is not None


class TagIndex:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.by_id = {row["tag_id"]: row for row in rows}
        self.facet = {row["tag_id"]: row["facet"] for row in rows}
        self.name = {row["tag_id"]: row["canonical_name"] for row in rows}
        self.parent = {row["tag_id"]: row["parent_id"] for row in rows if row.get("parent_id")}
        self.children: dict[str, list[str]] = defaultdict(list)
        for child, parent in self.parent.items():
            self.children[parent].append(child)
        self.alias_index: dict[str, tuple[str, str, str]] = {}
        for row in rows:
            keys = [row["canonical_name"]]
            keys.extend(alias for alias in (row.get("aliases") or "").split(";") if alias.strip())
            for alias in keys:
                key = normalized(alias)
                if key in self.alias_index and self.alias_index[key][0] != row["tag_id"]:
                    raise SystemExit(f"alias conflict: {alias} -> {self.alias_index[key]} / {row['tag_id']}")
                self.alias_index[key] = (row["tag_id"], alias.strip(), row["facet"])
        self.keys_by_length = sorted(self.alias_index, key=len, reverse=True)

    def match_phrase(
        self, phrase: str, facets: frozenset[str] | None = None
    ) -> list[tuple[str, str, str]]:
        """Return (tag_id, match_type, matched_alias) for one phrase.

        ``facets`` limits which tag dimensions the field is allowed to assign.
        Sector values must not produce stage or capital tags just because the
        source text happens to contain such a word.
        """
        key = normalized(phrase)
        if not key:
            return []
        exact = self.alias_index.get(key)
        if exact and (facets is None or exact[2] in facets):
            return [(exact[0], "exact", exact[1])]
        matches: list[tuple[str, str, str]] = []
        occupied: list[tuple[int, int]] = []
        for alias_key in self.keys_by_length:
            if len(alias_key) < 2:
                continue
            tag_id, alias, facet = self.alias_index[alias_key]
            if facets is not None and facet not in facets:
                continue
            start = key.find(alias_key)
            while start >= 0:
                end = start + len(alias_key)
                if not any(start < other_end and end > other_start for other_start, other_end in occupied):
                    occupied.append((start, end))
                    matches.append((tag_id, "alias", alias))
                start = key.find(alias_key, start + 1)
        if matches:
            return matches
        parts = [part for part in CONJUNCTION_SPLIT.split(key) if part.strip()]
        if len(parts) > 1:
            combined: list[tuple[str, str, str]] = []
            for part in parts:
                combined.extend(self.match_phrase(part, facets))
            if combined:
                return [(tag_id, "compound_contains", alias) for tag_id, _, alias in combined]
        return []


class TermIndex:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.term_id = {row["term"]: stable_id("term", row["term"]) for row in rows}
        self.name = {self.term_id[row["term"]]: row["term"] for row in rows}
        self.tag_ids = {
            self.term_id[row["term"]]: [value for value in row["tag_ids"].split(";") if value]
            for row in rows
        }
        self.key_to_term: dict[str, str] = {}
        for row in rows:
            for key in [row["term"], *[s for s in (row.get("synonyms") or "").split(",") if s.strip()]]:
                normalized_key = normalized(key)
                if not normalized_key:
                    continue
                if normalized_key in self.key_to_term and self.key_to_term[normalized_key] != self.term_id[row["term"]]:
                    raise SystemExit(f"term key conflict: {key}")
                self.key_to_term[normalized_key] = self.term_id[row["term"]]
        self.keys_by_length = sorted(self.key_to_term, key=len, reverse=True)

    def match_text(self, text: str) -> list[str]:
        key = normalized(text)
        if not key:
            return []
        found: list[str] = []
        for term_key in self.keys_by_length:
            if contains_key(key, term_key):
                identifier = self.key_to_term[term_key]
                if identifier not in found:
                    found.append(identifier)
        return found


def split_phrases(value: str) -> list[str]:
    return [part.strip() for part in PHASE_SPLIT.split(value or "") if part.strip()]


def build_release(args: argparse.Namespace) -> dict[str, Any]:
    release_id = getattr(args, "release_id", RELEASE_ID)
    names = output_names(release_id)
    evidence_files = [Path(path) for path in (getattr(args, "evidence_files", None) or [])]
    output_dir: Path = args.output_dir
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        raise SystemExit(f"output directory is not empty: {output_dir}; pass --overwrite to rebuild")
    output_dir.mkdir(parents=True, exist_ok=True)

    tag_rows = read_csv(args.tags)
    term_rows = read_csv(args.terms)
    tag_index = TagIndex(tag_rows)
    term_index = TermIndex(term_rows)
    for row in term_rows:
        for tag_id in row["tag_ids"].split(";"):
            if tag_id not in tag_index.by_id:
                raise SystemExit(f"term {row['term']} references unknown tag {tag_id}")

    investors, organizations = load_base_inputs(args.investors, args.organizations)
    org_map = {row["org_id"]: row for row in organizations}

    research_evidence: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    research_tags: dict[tuple[str, str], list[str]] = defaultdict(list)
    research_terms: dict[tuple[str, str], list[str]] = defaultdict(list)
    research_batches: list[dict[str, Any]] = []
    known_term_ids = set(term_index.term_id.values())

    def research_match_type(tag_id: str, evidence_text: str) -> str:
        text = normalized(evidence_text)
        canonical = normalized(tag_index.name.get(tag_id, ""))
        if canonical and canonical in text:
            return "research_exact"
        aliases = [
            normalized(alias)
            for alias in (tag_index.by_id[tag_id].get("aliases") or "").split(";")
            if alias.strip()
        ]
        if any(alias and alias in text for alias in aliases):
            return "research_alias"
        return "research_asserted"

    for evidence_file in evidence_files:
        if not evidence_file.is_file():
            raise SystemExit(f"research evidence file not found: {evidence_file}")
        batch_id = evidence_file.parent.name
        rows = read_csv(evidence_file)
        research_batches.append({
            "batch_id": batch_id,
            "file": str(evidence_file.relative_to(ROOT)) if evidence_file.is_relative_to(ROOT) else str(evidence_file),
            "sha256": sha256_file(evidence_file),
            "rows": len(rows),
        })
        for row in rows:
            entity_key = (row["entity_type"], row["entity_id"])
            if row.get("negative", "").lower() == "true":
                continue
            for tag_id in json.loads(row.get("tag_ids") or "[]"):
                if tag_id not in tag_index.by_id:
                    raise SystemExit(f"research evidence references unknown tag {tag_id}")
                research_evidence[entity_key].append({
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "tag_id": tag_id,
                    "canonical_name": tag_index.name[tag_id],
                    "facet": tag_index.facet[tag_id],
                    "assignment_type": row.get("assignment_type") or "derived",
                    "match_type": research_match_type(tag_id, row.get("evidence_text", "")),
                    "source_field": row.get("field", ""),
                    "evidence_text": clean_text(row.get("evidence_text", ""), 500),
                    "confidence": f"{float(row.get('confidence') or 0):.2f}",
                    "taxonomy_version": TAXONOMY_VERSION,
                    "term_id": "",
                    "source_url": row.get("source_url", ""),
                    "source_type": row.get("source_type", ""),
                    "researched_at": row.get("researched_at", ""),
                    "negative": "false",
                    "batch_id": row.get("batch_id") or batch_id,
                    "research_record_id": row.get("record_id", ""),
                })
            for term_id in json.loads(row.get("term_ids") or "[]"):
                if term_id not in known_term_ids:
                    raise SystemExit(f"research evidence references unknown term {term_id}")
                research_evidence[entity_key].append({
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "tag_id": "",
                    "canonical_name": term_index.name[term_id],
                    "facet": "term",
                    "assignment_type": row.get("assignment_type") or "derived",
                    "match_type": "term_research",
                    "source_field": row.get("field", ""),
                    "evidence_text": clean_text(row.get("evidence_text", ""), 500),
                    "confidence": f"{float(row.get('confidence') or 0):.2f}",
                    "taxonomy_version": TAXONOMY_VERSION,
                    "term_id": term_id,
                    "source_url": row.get("source_url", ""),
                    "source_type": row.get("source_type", ""),
                    "researched_at": row.get("researched_at", ""),
                    "negative": "false",
                    "batch_id": row.get("batch_id") or batch_id,
                    "research_record_id": row.get("record_id", ""),
                })
            for tag_id in json.loads(row.get("tag_ids") or "[]"):
                if tag_id not in research_tags[entity_key]:
                    research_tags[entity_key].append(tag_id)
            for term_id in json.loads(row.get("term_ids") or "[]"):
                if term_id not in research_terms[entity_key]:
                    research_terms[entity_key].append(term_id)

    unmapped_counter: Counter[tuple[str, str]] = Counter()
    unmapped_examples: dict[tuple[str, str], list[str]] = defaultdict(list)

    def note_unmapped(field: str, raw: str, entity_id: str) -> None:
        key = (field, raw.strip())
        if not key[1]:
            return
        unmapped_counter[key] += 1
        if len(unmapped_examples[key]) < 3:
            unmapped_examples[key].append(entity_id)

    def tag_evidence_for(
        entity_id: str, entity_type: str, field: str, value: str,
        assignment_type: str, facets: frozenset[str], researched_at: str = "",
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for phrase in split_phrases(value):
            for tag_id, match_type, alias in tag_index.match_phrase(phrase, facets):
                if tag_id in seen:
                    continue
                seen.add(tag_id)
                confidence = {"exact": 0.95, "alias": 0.9, "compound_contains": 0.8}[match_type]
                evidence.append({
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "tag_id": tag_id,
                    "canonical_name": tag_index.name[tag_id],
                    "facet": tag_index.facet[tag_id],
                    "assignment_type": assignment_type,
                    "match_type": match_type,
                    "source_field": field,
                    "evidence_text": clean_text(phrase, 200),
                    "confidence": f"{confidence:.2f}",
                    "taxonomy_version": TAXONOMY_VERSION,
                    "term_id": "",
                    "source_url": f"internal://base/{entity_type}/{entity_id}/{field}",
                    "source_type": "internal_snapshot",
                    "researched_at": researched_at,
                    "negative": "false",
                    "batch_id": BASE_BATCH_ID,
                    "research_record_id": "",
                })
        return evidence

    def term_ids_for(fields: list[tuple[str, str]]) -> list[str]:
        found: list[str] = []
        for _, value in fields:
            for term_id in term_index.match_text(clean_text(value, 4000)):
                if term_id not in found:
                    found.append(term_id)
        return found

    def dedupe_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the highest-confidence evidence row per entity+tag/term."""
        best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for item in evidence:
            key = (
                item["entity_type"], item["entity_id"],
                item.get("tag_id") or "", item.get("term_id") or "",
            )
            current = best.get(key)
            if current is None or float(item.get("confidence") or 0) > float(current.get("confidence") or 0):
                best[key] = item
        output: list[dict[str, Any]] = []
        for item in best.values():
            if "evidence_id" not in item:
                item["evidence_id"] = stable_id(
                    "ev",
                    f"{item['entity_type']}|{item['entity_id']}|{item.get('tag_id') or ''}|"
                    f"{item.get('term_id') or ''}|{item['evidence_text']}|{item['confidence']}",
                )
            output.append(item)
        return output

    evidence_rows: list[dict[str, Any]] = []
    organization_rows: list[dict[str, Any]] = []
    org_tag_cache: dict[str, list[str]] = {}
    org_term_cache: dict[str, list[str]] = {}

    for row in organizations:
        org_id = row["org_id"]
        researched_at = row.get("last_updated") or row.get("research_timestamp") or ""
        evidence = dedupe_evidence([
            *tag_evidence_for(
                org_id, "organization", "org_sector_focus", row.get("org_sector_focus", ""),
                "explicit", SECTOR_FACETS, researched_at,
            ),
            *tag_evidence_for(
                org_id, "organization", "org_investment_stage", row.get("org_investment_stage", ""),
                "explicit", STAGE_FACETS, researched_at,
            ),
            *research_evidence.get(("organization", org_id), []),
        ])
        evidence_rows.extend(evidence)
        tag_ids = [item["tag_id"] for item in evidence if item.get("tag_id")]
        terms = list(dict.fromkeys([
            *term_ids_for([
                ("org_sector_focus", row.get("org_sector_focus", "")),
                ("org_description", row.get("org_description", "")),
                ("org_notable_portfolio", row.get("org_notable_portfolio", "")),
            ]),
            *research_terms.get(("organization", org_id), []),
        ]))
        org_tag_cache[org_id] = tag_ids
        org_term_cache[org_id] = terms
        for phrase in split_phrases(row.get("org_sector_focus", "")):
            if not tag_index.match_phrase(phrase, SECTOR_FACETS) and not term_index.match_text(phrase):
                note_unmapped("org_sector_focus", phrase, org_id)
        sector_text = " ".join(filter(None, [
            f"机构原始赛道:{clean_text(row.get('org_sector_focus', ''))}" if row.get("org_sector_focus") else "",
            "规范标签:" + ";".join(tag_index.name[t] for t in tag_ids) if tag_ids else "",
        ]))
        profile_text = " ".join(filter(None, [
            f"机构简介:{clean_text(row.get('org_description', ''))}" if row.get("org_description") else "",
            f"代表项目:{clean_text(row.get('org_notable_portfolio', ''))}" if row.get("org_notable_portfolio") else "",
        ]))
        organization_rows.append({
            "org_id": org_id,
            "chinese_name": row.get("chinese_name", ""),
            "english_name": row.get("english_name", ""),
            "name_search_text": " ".join(filter(None, [row.get("chinese_name", ""), row.get("english_name", ""), row.get("other_name", "")])),
            "sector_search_text": sector_text,
            "profile_search_text": profile_text,
            "tag_ids": json_cell(tag_ids),
            "tag_names": json_cell(tag_index.name[t] for t in tag_ids),
            "term_ids": json_cell(terms),
            "term_names": json_cell(term_index.name[t] for t in terms),
            "stage_raw": row.get("org_investment_stage", ""),
            "org_data_confidence": row.get("org_data_confidence", ""),
            "org_verification_status": row.get("org_verification_status", ""),
            "taxonomy_version": TAXONOMY_VERSION,
            "schema_version": SCHEMA_VERSION,
        })

    investor_rows: list[dict[str, Any]] = []
    for row in investors:
        investor_id = row["source_user_id"]
        org_id = row.get("org_id", "")
        org = org_map.get(org_id, {})
        researched_at = row.get("last_updated") or row.get("research_timestamp") or ""
        own_evidence = tag_evidence_for(
            investor_id, "investor", "direction", row.get("direction", ""),
            "explicit", PERSON_FACETS, researched_at,
        )
        own_evidence.extend(tag_evidence_for(
            investor_id, "investor", "person_expertise", row.get("person_expertise", ""),
            "derived", PERSON_FACETS, researched_at,
        ))
        own_evidence.extend(tag_evidence_for(
            investor_id, "investor", "person_investment_preferences",
            row.get("person_investment_preferences", ""), "derived", PERSON_FACETS, researched_at,
        ))
        own_evidence.extend(research_evidence.get(("investor", investor_id), []))
        own_evidence = dedupe_evidence(own_evidence)
        evidence_rows.extend(own_evidence)
        own_tag_ids = [item["tag_id"] for item in own_evidence if item.get("tag_id")]
        inherited_tag_ids = org_tag_cache.get(org_id, [])
        for tag_id in inherited_tag_ids:
            if tag_id in own_tag_ids:
                continue
            evidence_rows.append({
                "entity_type": "investor",
                "entity_id": investor_id,
                "tag_id": tag_id,
                "canonical_name": tag_index.name[tag_id],
                "facet": tag_index.facet[tag_id],
                "assignment_type": "inherited",
                "match_type": "inherited",
                "source_field": "org_id",
                "evidence_text": org.get("chinese_name", "") or org_id,
                "confidence": "0.72",
                "taxonomy_version": TAXONOMY_VERSION,
                "term_id": "",
                "source_url": f"internal://base/investor/{investor_id}/org_id",
                "source_type": "internal_snapshot",
                "researched_at": researched_at,
                "negative": "false",
                "batch_id": BASE_BATCH_ID,
                "research_record_id": "",
            })
        own_terms = list(dict.fromkeys([
            *term_ids_for([
                ("direction", row.get("direction", "")),
                ("person_expertise", row.get("person_expertise", "")),
                ("person_investment_preferences", row.get("person_investment_preferences", "")),
                ("person_background", row.get("person_background", "")),
                ("person_representative_deals", row.get("person_representative_deals", "")),
            ]),
            *research_terms.get(("investor", investor_id), []),
        ]))
        inherited_terms = org_term_cache.get(org_id, [])
        all_terms = list(dict.fromkeys([*own_terms, *inherited_terms]))
        all_tags = list(dict.fromkeys([*own_tag_ids, *inherited_tag_ids]))
        for field in ("direction", "person_expertise"):
            for phrase in split_phrases(row.get(field, "")):
                if not tag_index.match_phrase(phrase, PERSON_FACETS) and not term_index.match_text(phrase):
                    note_unmapped(field, phrase, investor_id)
        name_text = " ".join(filter(None, [
            row.get("name", ""), org.get("chinese_name", "") or row.get("org_name_matched", ""), row.get("position", ""),
        ]))
        sector_text = " ".join(filter(None, [
            f"机构原始赛道:{clean_text(row.get('org_sector_focus', ''))}" if row.get("org_sector_focus") else "",
            "规范标签:" + ";".join(tag_index.name[t] for t in all_tags) if all_tags else "",
            f"个人方向:{clean_text(row.get('direction', ''))}" if row.get("direction") else "",
            f"个人专长:{clean_text(row.get('person_expertise', ''))}" if row.get("person_expertise") else "",
            f"投资偏好:{clean_text(row.get('person_investment_preferences', ''))}" if row.get("person_investment_preferences") else "",
        ]))
        profile_text = " ".join(filter(None, [
            f"机构简介:{clean_text(org.get('org_description', ''))}" if org.get("org_description") else "",
            f"代表项目:{clean_text(org.get('org_notable_portfolio', ''))}" if org.get("org_notable_portfolio") else "",
            f"个人背景:{clean_text(row.get('person_background', ''))}" if row.get("person_background") else "",
            f"代表案例:{clean_text(row.get('person_representative_deals', ''))}" if row.get("person_representative_deals") else "",
        ]))
        investor_rows.append({
            "source_user_id": investor_id,
            "org_id": org_id,
            "name": row.get("name", ""),
            "org_name_authoritative": org.get("chinese_name", "") or row.get("org_name_matched", ""),
            "position": row.get("position", ""),
            "currency": row.get("currency", ""),
            "inv_min": row.get("inv_min", ""),
            "inv_max": row.get("inv_max", ""),
            "compliance_overall": row.get("compliance_overall", ""),
            "person_data_confidence": row.get("person_data_confidence", ""),
            "person_verification_status": row.get("person_verification_status", ""),
            "own_tag_ids": json_cell(own_tag_ids),
            "inherited_tag_ids": json_cell(inherited_tag_ids),
            "all_tag_ids": json_cell(all_tags),
            "all_tag_names": json_cell(tag_index.name[t] for t in all_tags),
            "term_ids": json_cell(all_terms),
            "term_names": json_cell(term_index.name[t] for t in all_terms),
            "name_search_text": name_text,
            "sector_search_text": sector_text,
            "profile_search_text": profile_text,
            "stage_raw": row.get("org_investment_stage", ""),
            "taxonomy_version": TAXONOMY_VERSION,
            "schema_version": SCHEMA_VERSION,
        })

    for row in evidence_rows:
        if not row.get("evidence_id"):
            row["evidence_id"] = stable_id(
                "ev",
                f"{row['entity_type']}|{row['entity_id']}|{row.get('tag_id') or ''}|"
                f"{row.get('term_id') or ''}|{row['evidence_text']}|{row['confidence']}",
            )

    tag_output = [{
        "tag_id": row["tag_id"],
        "facet": row["facet"],
        "group": row.get("group", ""),
        "canonical_name": row["canonical_name"],
        "aliases": row.get("aliases", ""),
        "parent_id": row.get("parent_id", ""),
        "status": "active",
        "taxonomy_version": TAXONOMY_VERSION,
    } for row in tag_rows]

    term_output = [{
        "term_id": term_index.term_id[row["term"]],
        "term": row["term"],
        "synonyms": row.get("synonyms", ""),
        "tag_ids": row["tag_ids"],
        "group": row.get("group", ""),
        "status": "active",
        "taxonomy_version": TAXONOMY_VERSION,
    } for row in term_rows]

    relation_rows: list[dict[str, Any]] = []
    for child, parent in sorted(tag_index.parent.items()):
        relation_rows.append({
            "source_tag_id": child,
            "target_tag_id": parent,
            "relation_type": "is_a",
            "weight": "0.90",
            "source": "taxonomy_parent",
            "taxonomy_version": TAXONOMY_VERSION,
        })
    related_pairs = [
        ("sector.robotics.embodied", "sector.ai", "related", "0.70"),
        ("sector.ai.chip", "sector.semiconductor", "related", "0.75"),
        ("sector.health.synthetic_biology", "sector.health.biotech", "related", "0.80"),
        ("sector.energy.fusion", "sector.energy.new", "related", "0.80"),
        ("sector.health.bci", "sector.health.digital", "related", "0.70"),
        ("sector.mobility.autonomous", "sector.ai.application", "related", "0.70"),
        ("sector.manufacturing.smart", "sector.ai.application", "related", "0.65"),
        ("sector.aerospace.drone", "sector.aerospace.low_altitude", "related", "0.80"),
    ]
    for source, target, relation_type, weight in related_pairs:
        if source in tag_index.by_id and target in tag_index.by_id:
            relation_rows.append({
                "source_tag_id": source, "target_tag_id": target,
                "relation_type": relation_type, "weight": weight,
                "source": "curated_v2", "taxonomy_version": TAXONOMY_VERSION,
            })

    unmapped_rows = [{
        "field": field, "raw_term": raw, "count": count,
        "example_entity_ids": json_cell(unmapped_examples[(field, raw)]),
        "taxonomy_version": TAXONOMY_VERSION,
    } for (field, raw), count in sorted(unmapped_counter.items(), key=lambda item: (-item[1], item[0]))]

    data_sets = {
        "tags": (tag_output, ["tag_id", "facet", "group", "canonical_name", "aliases", "parent_id", "status", "taxonomy_version"]),
        "terms": (term_output, ["term_id", "term", "synonyms", "tag_ids", "group", "status", "taxonomy_version"]),
        "relations": (relation_rows, ["source_tag_id", "target_tag_id", "relation_type", "weight", "source", "taxonomy_version"]),
        "evidence": (evidence_rows, EVIDENCE_FIELDS),
        "investors": (investor_rows, list(investor_rows[0].keys())),
        "organizations": (organization_rows, list(organization_rows[0].keys())),
        "unmapped": (unmapped_rows, ["field", "raw_term", "count", "example_entity_ids", "taxonomy_version"]),
    }
    output_stats: dict[str, dict[str, Any]] = {}
    for key, (rows, fields) in data_sets.items():
        path = output_dir / names[key]
        row_count, column_count = write_csv(path, fields, rows)
        output_stats[path.name] = {
            "sha256": sha256_file(path), "rows": row_count, "columns": column_count,
        }

    tag_coverage = {
        "organizations_with_tags": sum(1 for row in organization_rows if row["tag_ids"] != "[]"),
        "investors_with_tags": sum(1 for row in investor_rows if row["all_tag_ids"] != "[]"),
        "investors_with_own_tags": sum(1 for row in investor_rows if row["own_tag_ids"] != "[]"),
        "investors_with_terms": sum(1 for row in investor_rows if row["term_ids"] != "[]"),
    }
    def relative_input(path: Path) -> str:
        try:
            return path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return path.name

    research_evidence_rows = sum(1 for row in evidence_rows if row.get("batch_id") != BASE_BATCH_ID)
    manifest = {
        "release_id": release_id,
        "parent_release_id": getattr(args, "parent_release_id", None),
        "schema_version": SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "rule_version": "v3-rule/1.0",
        "inputs": {
            relative_input(args.investors): sha256_file(args.investors),
            relative_input(args.organizations): sha256_file(args.organizations),
            relative_input(args.tags): sha256_file(args.tags),
            relative_input(args.terms): sha256_file(args.terms),
        },
        "research_batches": research_batches,
        "outputs": output_stats,
        "counts": {
            "tags": len(tag_output),
            "terms": len(term_output),
            "relations": len(relation_rows),
            "evidence": len(evidence_rows),
            "evidence_research": research_evidence_rows,
            "investors": len(investor_rows),
            "organizations": len(organization_rows),
            "unmapped_terms": len(unmapped_rows),
        },
        "coverage": tag_coverage,
    }
    manifest_path = output_dir / names["manifest"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the v2 investor search release")
    parser.add_argument("--investors", type=Path, default=DEFAULT_INVESTORS)
    parser.add_argument("--organizations", type=Path, default=DEFAULT_ORGANIZATIONS)
    parser.add_argument("--tags", type=Path, default=DEFAULT_TAGS)
    parser.add_argument("--terms", type=Path, default=DEFAULT_TERMS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--release-id", default=RELEASE_ID)
    parser.add_argument("--parent-release-id", default=None)
    parser.add_argument("--evidence-file", action="append", dest="evidence_files",
                        help="normalized_records.csv from an ingested research batch (repeatable)")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = build_release(args)
    except (OSError, csv.Error, KeyError) as error:
        print(f"build failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps({
        "release_id": manifest["release_id"],
        "counts": manifest["counts"],
        "coverage": manifest["coverage"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
