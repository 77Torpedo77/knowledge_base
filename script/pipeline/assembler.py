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
]


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

    # 附加有用字段
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
    """
    拼装最终输出，格式匹配 json5示例.json。

    Args:
        llm_result: LLM 原始输出（含 section_mapping）
        verified_entities: 校验后的 extracted_entities
        indexed_blocks: 原始文本块
        zotero_meta: Zotero 元数据

    Returns:
        符合 json5示例 格式的最终 dict
    """
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
    total_entities = sum(
        len(llm_result.get("extracted_entities", {}).get(cat, []))
        for cat in ("research_tasks", "proposed_methods", "datasets",
                     "evaluation_metrics", "baselines",
                     "addressed_existing_flaws", "self_admitted_limitations")
    )
    verified_count = sum(len(v) for v in verified_entities.values())

    log.info(
        "[%s] Assembly done — Sections: %d | Entities: %d/%d verified | TRASH: %d",
        cite_key, len(sections), verified_count, total_entities, trash_count,
    )

    return {
        "paper_id": cite_key,
        "metadata": metadata,
        "sections": sections,
        "extracted_entities": verified_entities,
    }
