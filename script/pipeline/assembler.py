"""Phase 4: 结果拼装 — 将各阶段结果拼装为最终 json5示例 格式"""

import logging

log = logging.getLogger(__name__)

VALID_SECTIONS = [
    "ABSTRACT",
    "MOTIVATION_AND_BACKGROUND",
    "RELATED_WORK",
    "METHODOLOGY",
    "EXPERIMENT_SETUP_AND_RESULTS",
    "DISCUSSION_AND_CONCLUSION",
    "APPENDIX_AND_SUPPLEMENTARY",
    "OTHER",
]

ALL_SECTION_KEYS = set(VALID_SECTIONS) | {"TRASH"}

ENTITY_CATEGORIES = [
    "research_tasks",
    "proposed_methods",
    "datasets",
    "evaluation_metrics",
    "baselines",
    "addressed_existing_flaws",
    "self_admitted_limitations",
]

DATASET_USAGE_ROLES = {"pre-training", "fine-tuning", "evaluation", "other"}
REQUIRED_ENTITY_FIELDS = {"name_in_paper", "aliases", "semantic_definition",
                          "evidence_quote", "evidence_block_id"}


def validate_llm_output(llm_result: dict):
    """严格校验 LLM 输出是否符合 schema，不符合则抛出 ValueError。"""
    # === section_mapping ===
    mapping = llm_result.get("section_mapping")
    if not isinstance(mapping, dict):
        raise ValueError(f"section_mapping missing or not dict: {type(mapping)}")
    if set(mapping.keys()) != ALL_SECTION_KEYS:
        missing = ALL_SECTION_KEYS - set(mapping.keys())
        extra = set(mapping.keys()) - ALL_SECTION_KEYS
        raise ValueError(f"section_mapping keys mismatch: missing={missing}, extra={extra}")
    for key, ids in mapping.items():
        if not isinstance(ids, list) or not all(isinstance(x, int) for x in ids):
            raise ValueError(f"section_mapping['{key}'] must be int[], got {type(ids)}")

    # === extracted_entities ===
    entities = llm_result.get("extracted_entities")
    if not isinstance(entities, dict):
        raise ValueError(f"extracted_entities missing or not dict: {type(entities)}")
    if set(entities.keys()) != set(ENTITY_CATEGORIES):
        missing = set(ENTITY_CATEGORIES) - set(entities.keys())
        extra = set(entities.keys()) - set(ENTITY_CATEGORIES)
        raise ValueError(f"extracted_entities keys mismatch: missing={missing}, extra={extra}")

    # === 逐实体校验 ===
    for cat, ents in entities.items():
        if not isinstance(ents, list):
            raise ValueError(f"extracted_entities['{cat}'] must be list, got {type(ents)}")
        for i, ent in enumerate(ents):
            if not isinstance(ent, dict):
                raise ValueError(f"{cat}[{i}] must be dict, got {type(ent)}")
            missing = REQUIRED_ENTITY_FIELDS - set(ent.keys())
            if missing:
                raise ValueError(f"{cat}[{i}] '{ent.get('name_in_paper', '?')}' missing fields: {missing}")
            if not isinstance(ent["aliases"], list):
                raise ValueError(f"{cat}[{i}] '{ent['name_in_paper']}' aliases must be list")
            if not isinstance(ent["evidence_block_id"], int):
                raise ValueError(f"{cat}[{i}] '{ent['name_in_paper']}' evidence_block_id must be int")
            if cat == "datasets" and ent.get("usage_role") not in DATASET_USAGE_ROLES:
                raise ValueError(f"datasets[{i}] '{ent['name_in_paper']}' usage_role must be one of {DATASET_USAGE_ROLES}, got '{ent.get('usage_role')}'")
            if cat == "addressed_existing_flaws" and not ent.get("targeted_baseline"):
                raise ValueError(f"addressed_existing_flaws[{i}] '{ent['name_in_paper']}' missing targeted_baseline")

    log.info("LLM schema validation: passed")


def _build_metadata(zotero_meta: dict) -> dict:
    """从 Zotero 元数据构建 json5示例 格式的 metadata。"""
    creators = zotero_meta.get("creators", [])
    authors = []
    for c in creators:
        if c.get("creatorType") == "author":
            first = c.get("firstName", "")
            last = c.get("lastName", "")
            authors.append(f"{first} {last}".strip())

    date_str = zotero_meta.get("date", "")
    year = None
    if date_str:
        try:
            year = int(date_str.split("/")[0])
        except (ValueError, IndexError):
            pass

    meta = {
        "title": zotero_meta.get("title", ""),
        "authors": authors,
        "publication_year": year,
        "venue": zotero_meta.get("publicationTitle", ""),
    }

    for key in ("DOI", "abstractNote", "language", "url", "volume", "issue",
                "pages", "itemType", "publisher", "ISSN"):
        val = zotero_meta.get(key)
        if val:
            meta[key] = val

    return meta


def check_coverage(llm_result: dict, indexed_blocks: list[dict]) -> tuple[list[int], list[int]]:
    """检查 section_mapping 是否完整覆盖所有 block ID。

    Returns:
        (missing_ids, duplicate_ids) — 缺失和重复的 block ID 列表。
    """
    expected_ids = set(b["id"] for b in indexed_blocks)
    assigned_ids = []
    for ids in llm_result.get("section_mapping", {}).values():
        assigned_ids.extend(ids)
    assigned_set = set(assigned_ids)
    missing = sorted(expected_ids - assigned_set)
    duplicates = sorted(set(bid for bid in assigned_ids if assigned_ids.count(bid) > 1))
    return missing, duplicates


def assemble(llm_result: dict, verified_entities: dict,
             indexed_blocks: list[dict], zotero_meta: dict) -> dict:
    """拼装最终输出，格式匹配 json5示例.json。"""
    cite_key = zotero_meta.get("cite_key", "unknown")
    section_mapping = llm_result.get("section_mapping", {})
    block_map = {b["id"]: b["text"] for b in indexed_blocks}

    # 1. Sections
    sections = []
    for sec_type in VALID_SECTIONS:
        ids = section_mapping.get(sec_type, [])
        parts = [block_map[bid] for bid in ids if bid in block_map]
        content = "\n".join(parts)
        if content:
            sections.append({"type": sec_type, "content": content})

    # 2. Metadata
    metadata = _build_metadata(zotero_meta)

    # 3. 统计
    trash_count = len(section_mapping.get("TRASH", []))
    appendix_count = len(section_mapping.get("APPENDIX_AND_SUPPLEMENTARY", []))
    other_count = len(section_mapping.get("OTHER", []))
    total_entities = sum(
        len(llm_result.get("extracted_entities", {}).get(cat, []))
        for cat in ENTITY_CATEGORIES
    )
    verified_count = sum(len(v) for v in verified_entities.values())

    log.info(
        "[%s] Assembly done — Sections: %d | Entities: %d/%d verified | TRASH: %d | APPENDIX: %d | OTHER: %d",
        cite_key, len(sections), verified_count, total_entities,
        trash_count, appendix_count, other_count,
    )

    return {
        "paper_id": cite_key,
        "metadata": metadata,
        "sections": sections,
        "extracted_entities": verified_entities,
    }
