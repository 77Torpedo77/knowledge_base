"""Phase 3: 零幻觉校验 — 基于 evidence_block_id 精确校验 evidence_quote"""

import logging

from thefuzz import fuzz

log = logging.getLogger(__name__)

_ENTITY_CATEGORIES = [
    "research_tasks",
    "proposed_methods",
    "datasets",
    "evaluation_metrics",
    "baselines",
    "addressed_existing_flaws",
    "self_admitted_limitations",
]


def _verify_entity(entity: dict, block_map: dict) -> tuple[bool, str | None]:
    """
    基于 evidence_block_id 校验 evidence_quote。
    返回 (是否通过, 替换后的quote 或 None 表示应丢弃)。
    """
    block_id = entity.get("evidence_block_id")
    quote = entity.get("evidence_quote", "")

    if not quote:
        return False, None

    # block_id 无效，尝试全量搜索兜底
    if block_id is None or block_id not in block_map:
        log.warning("Entity '%s' has invalid evidence_block_id=%s, fallback to full search",
                    entity.get("name_in_paper"), block_id)
        for block in block_map.values():
            if quote in block["text"]:
                return True, quote
        log.warning("HALLUCINATION: '%s' quote='%.60s...' not found, DROPPED",
                    entity.get("name_in_paper"), quote)
        return False, None

    original_text = block_map[block_id]["text"]

    # 精确匹配（大小写不敏感）
    if quote.lower() in original_text.lower():
        return True, quote

    # 模糊匹配（partial_ratio 滑动窗口找最佳子串匹配）
    best_ratio = fuzz.partial_ratio(quote, original_text) / 100.0
    if best_ratio >= 0.85:
        log.info("Fuzzy match for '%s': %.1f%% → accepted",
                 entity.get("name_in_paper"), best_ratio * 100)
        return True, quote

    log.warning("HALLUCINATION: '%s' block_id=%d quote='%.60s...' partial_ratio=%.1f%%, DROPPED",
                entity.get("name_in_paper"), block_id, quote, best_ratio * 100)
    return False, None


def verify_entities(raw_entities: dict, indexed_blocks: list[dict]) -> dict:
    """
    校验 extracted_entities 中所有类别的 evidence_quote。
    返回校验后的 extracted_entities dict，丢弃未通过的实体。
    """
    block_map = {b["id"]: b for b in indexed_blocks}
    verified = {}
    total = 0
    passed = 0

    for category in _ENTITY_CATEGORIES:
        entities = raw_entities.get(category, [])
        if not entities:
            verified[category] = []
            continue

        cat_passed = []
        for entity in entities:
            total += 1
            ok, replacement = _verify_entity(entity, block_map)
            if ok:
                entity["evidence_quote"] = replacement
                cat_passed.append(entity)
                passed += 1

        verified[category] = cat_passed

    log.info("Evidence verification: %d/%d entities passed", passed, total)
    return verified
