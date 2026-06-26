#!/usr/bin/env python3
"""
Gold Layer 增量处理：逐 Mention 构建 CanonicalEntity + RESOLVES_TO + BELONGS_TO。

处理所有无 RESOLVES_TO 的 Mention（包括新论文入库后的新 Mention）。
对 Silver 合并组，每组作为整体进行一次 LLM 决策。

用法:
  python gold_incremental.py --dry-run                     # 预览
  python gold_incremental.py                               # 执行（增量，跳过已处理的）
  python gold_incremental.py --force-reprocess              # 清除已有 RESOLVES_TO 重新处理
  python gold_incremental.py --batch-size 5                 # 自定义批大小
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from neo4j import GraphDatabase
from openai import OpenAI

from gold_embedder import Embedder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

MENTION_LABELS = [
    "TaskMention",
    "MethodMention",
    "DatasetMention",
    "MetricMention",
    "BaselineMention",
    "FlawMention",
    "LimitationMention",
]

CANONICAL_MAP = {
    "TaskMention": "CanonicalTask",
    "MethodMention": "CanonicalMethod",
    "DatasetMention": "CanonicalDataset",
    "MetricMention": "CanonicalMetric",
    "BaselineMention": "CanonicalBaseline",
    "FlawMention": "CanonicalFlaw",
    "LimitationMention": "CanonicalLimitation",
}

SYSTEM_PROMPT = """\
You are a rigorous academic entity resolution expert for SLAM / visual odometry / robotics research.

You will receive:
1. A batch of MENTIONS (up to 5): named entities extracted from research papers
2. TOP-5 CANDIDATE canonical entities for each Mention (if available)
3. The FULL LIST of existing categories

YOUR TASK: For EACH Mention, choose ONE of three actions:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACTION A: SAME_AS
Use when the Mention is semantically IDENTICAL to one of the Top-5 candidates.
Output: {"action": "SAME_AS", "canonical_id": "<id_of_existing_canonical>"}

STRICT rules:
  - "KITTI" and "KITTI Odometry" → SAME_AS (same dataset)
  - "ATE" and "Absolute Trajectory Error" → SAME_AS (same metric)
  - "DSO" and "Direct Sparse Odometry" → SAME_AS (same algorithm)
  - "ORB-SLAM2" and "ORB-SLAM3" → DIFFERENT (different versions!)
  - "ResNet-50" and "ResNet-101" → DIFFERENT (different architectures!)
  - "rotation error" and "translation error" → DIFFERENT (different metrics!)

ACTION B: NEW + Existing Category
Use when Mention does NOT match any candidate, but fits into an existing Category.
Output: {"action": "NEW", "category": "<existing_category_name>", "canonical_name": "<normalized_name>"}
Choose the most specific appropriate category from the existing list.

ACTION C: NEW + New Category
Use ONLY when no existing category adequately covers this entity.
Output: {"action": "NEW", "category": "<new_category_name>", "category_description": "<1-sentence>", "canonical_name": "<normalized_name>"}
The new category should be at the same level of abstraction as existing ones.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RULES FOR canonical_name:
- Use the most widely recognized academic name
- Prefer full names over abbreviations (e.g., "Direct Sparse Odometry" over "DSO")
- But use abbreviations when they are the de facto standard (e.g., "ORB-SLAM3", "VINS-Fusion")

OUTPUT: A JSON array, one entry per Mention in the batch:
{
  "decisions": [
    {"mention_index": 0, "action": "SAME_AS", "canonical_id": "..."},
    {"mention_index": 1, "action": "NEW", "category": "SLAM", "canonical_name": "MyNewMethod"},
    ...
  ]
}
"""


def fetch_categories(session) -> dict[str, list[dict]]:
    """按 mention_label 加载所有 Category。"""
    categories: dict[str, list[dict]] = {}
    result = session.run(
        "MATCH (c:Category) RETURN c.name AS name, c.description AS description, "
        "c.mention_label AS mention_label"
    )
    for r in result:
        label = r["mention_label"]
        categories.setdefault(label, []).append({
            "name": r["name"],
            "description": r["description"],
        })
    return categories


def fetch_canonical_entities(session) -> dict[str, list[dict]]:
    """按 Label 加载所有 CanonicalEntity（含 embedding）。"""
    entities: dict[str, list[dict]] = defaultdict(list)
    for mention_label, canonical_label in CANONICAL_MAP.items():
        try:
            result = session.run(
                f"MATCH (ce:{canonical_label}) "
                f"WHERE ce.embedding IS NOT NULL "
                f"RETURN ce.canonical_id AS canonical_id, ce.canonical_name AS canonical_name, "
                f"ce.embedding AS embedding"
            )
            for r in result:
                entities[mention_label].append({
                    "canonical_id": r["canonical_id"],
                    "canonical_name": r["canonical_name"],
                    "embedding": r["embedding"],
                })
        except Exception:
            # Canonical* 节点类型可能还不存在（冷启动后首次运行）
            pass
    return entities


def load_silver_groups(path: str | None = None) -> list[dict]:
    """加载 Silver 合并组。"""
    if path is None:
        path = str(Path(__file__).resolve().parent.parent / "silver_merge_review.json")
    if not os.path.exists(path):
        log.warning("Silver merge review not found: %s", path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        silver_data = json.load(f)
    groups = []
    for label, label_data in silver_data.items():
        if not isinstance(label_data, dict):
            continue
        for grp in label_data.get("groups", []):
            members = grp.get("members", [])
            if len(members) < 2:
                continue
            groups.append({
                "is_silver_group": True,
                "label": label,
                "member_eids": [m["mention_id"] for m in members],
                "member_names": [m["name"] for m in members],
                "representative_name": members[0]["name"],
                "representative_aliases": members[0].get("aliases", []),
            })
    log.info("Loaded %d Silver merge groups", len(groups))
    return groups


def fetch_unresolved_mentions(session) -> list[dict]:
    """查询所有无 RESOLVES_TO 的 Mention。"""
    mentions = []
    for label in MENTION_LABELS:
        try:
            result = session.run(
                f"MATCH (m:{label}) "
                f"WHERE NOT (m)-[:RESOLVES_TO]->() "
                f"RETURN elementId(m) AS eid, m.name AS name, "
                f"m.aliases AS aliases, m.semantic_definition AS definition"
            )
            for r in result:
                mentions.append({
                    "eid": r["eid"],
                    "name": r["name"],
                    "aliases": r["aliases"] or [],
                    "definition": r["definition"] or "",
                    "label": label,
                    "is_silver_group": False,
                })
        except Exception as e:
            log.warning("[%s] fetch failed: %s", label, e)
    log.info("Fetched %d unresolved Mention nodes", len(mentions))
    return mentions


def build_work_queue(
    session,
    silver_path: str | None = None,
) -> list[dict]:
    """构建处理队列：Silver 合并组 + 未分组 Mention。"""
    silver_groups = load_silver_groups(silver_path)
    mentions = fetch_unresolved_mentions(session)

    # 将 Silver group 成员从独立 mentions 中排除（group 整体处理）
    silver_eid_set: set[str] = set()
    for grp in silver_groups:
        silver_eid_set.update(grp["member_eids"])

    queue = list(silver_groups)
    for m in mentions:
        if m["eid"] not in silver_eid_set:
            queue.append(m)

    log.info("Work queue: %d total (%d Silver groups + %d individual)",
             len(queue), len(silver_groups), len(queue) - len(silver_groups))
    return queue


def build_candidate_lookup(canonical_entities: dict[str, list[dict]], embedder: Embedder) -> dict[str, np.ndarray]:
    """构建候选向量查找表。"""
    lookup: dict[str, np.ndarray] = {}
    for label, entities in canonical_entities.items():
        for ce in entities:
            emb = ce.get("embedding")
            if emb:
                lookup[ce["canonical_id"]] = np.asarray(emb, dtype=np.float32)
    return lookup


def search_top_k(
    embedder: Embedder,
    mention_name: str,
    canonical_entities: dict[str, list[dict]],
    mention_label: str,
    top_k: int = 5,
) -> list[dict]:
    """在对应 Label 的 CanonicalEntity 中搜索 Top-K。"""
    candidates = canonical_entities.get(mention_label, [])
    if not candidates:
        return []
    query_vec = embedder.embed_single(mention_name)
    return embedder.search_top_k(query_vec, candidates, top_k)


def batch_candidates(work_items: list[dict], canonical_entities: dict[str, list[dict]], embedder: Embedder):
    """为一批 work items 准备 Top-5 候选。"""
    for item in work_items:
        label = item["label"]
        name = item.get("representative_name") or item["name"]
        item["top5"] = search_top_k(embedder, name, canonical_entities, label)


def stream_llm_response(client: OpenAI, prompt: str, content: str, model: str) -> dict | None:
    """流式调用 LLM，返回解析后的 JSON。"""
    max_retries = 2
    for attempt in range(1, max_retries + 1):
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": content},
                ],
                response_format={"type": "json_object"},
                max_tokens=32768,
                reasoning_effort="max",
                extra_body={"thinking": {"type": "enabled"}},
                stream=True,
                timeout=900,
            )

            content_parts = []
            usage = None
            for chunk in stream:
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = chunk.usage
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    content_parts.append(delta.content)

            response_text = "".join(content_parts)
            if usage:
                log.info("  LLM tokens: %s/%s", usage.prompt_tokens, usage.completion_tokens)

            if not response_text.strip():
                log.error("LLM returned empty response (attempt %d/%d)", attempt, max_retries)
                continue

            return json.loads(response_text)

        except json.JSONDecodeError as e:
            log.error("JSON parse failed (attempt %d/%d): %s", attempt, max_retries, e)
            continue
        except Exception as e:
            log.error("API call failed (attempt %d/%d): %s", attempt, max_retries, e)
            time.sleep(5)
            continue

    return None


def make_llm_prompt(items: list[dict], categories: dict[str, list[dict]]) -> str:
    """构造 LLM prompt 内容（categories 按 mention_label 索引）。"""
    parts = []

    # Mentions
    parts.append("MENTIONS TO RESOLVE:")
    for i, item in enumerate(items):
        name = item.get("representative_name") or item["name"]
        label = item["label"]
        is_grp = item.get("is_silver_group", False)
        if is_grp:
            parts.append(f"\n  [{i}] (Silver Group, {label})")
            parts.append(f"      representative: {name}")
            parts.append(f"      all names: {item['member_names']}")
        else:
            parts.append(f"\n  [{i}] ({label})")
            parts.append(f"      name: {name}")
            if item.get("aliases"):
                parts.append(f"      aliases: {item['aliases']}")
        if item.get("definition"):
            parts.append(f"      definition: {item['definition'][:300]}")

        # Top-5 candidates
        top5 = item.get("top5", [])
        if top5:
            parts.append(f"      Top-5 candidates:")
            for c in top5:
                parts.append(f"        {c['canonical_id']}: {c['canonical_name']} (sim={c['similarity']:.3f})")
        else:
            parts.append(f"      Top-5 candidates: (none yet — first entities in this category)")

    # Categories (只展示该 Mention Label 对应的分类)
    label_categories = categories.get(items[0]["label"], []) if items else []
    parts.append("\nEXISTING CATEGORIES (for this mention type):")
    if label_categories:
        for c in label_categories:
            parts.append(f"  - {c['name']}: {c['description']}")
    else:
        parts.append("  (none yet — create new as needed)")

    parts.append("\nFor each of the mentions [{0}..{1}], output a decision in the decisions array.".format(0, len(items) - 1))
    return "\n".join(parts)


def execute_decisions(
    session,
    items: list[dict],
    decisions: list[dict],
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """执行 LLM 决策，写入 Neo4j。返回 (same_as, new_existing, new_newcat)。"""
    stats = [0, 0, 0]  # [same_as, new+exist_category, new+new_category]
    now = datetime.now(timezone.utc).isoformat()

    for dec in decisions:
        idx = dec.get("mention_index", -1)
        if idx < 0 or idx >= len(items):
            log.warning("Invalid mention_index: %d", idx)
            continue
        item = items[idx]
        action = dec.get("action", "")
        label = item.get("label", "")

        if action == "SAME_AS":
            canonical_id = dec.get("canonical_id", "")
            if not canonical_id:
                log.warning("SAME_AS missing canonical_id for item %d", idx)
                continue

            if not dry_run:
                eids = item.get("member_eids", [item.get("eid")])
                canonical_type = CANONICAL_MAP.get(label, "CanonicalEntity")
                for eid in eids:
                    session.run(
                        f"""
                        MATCH (m)
                        WHERE elementId(m) = $eid
                        MATCH (ce:{canonical_type} {{canonical_id: $canonical_id}})
                        MERGE (m)-[:RESOLVES_TO]->(ce)
                        """,
                        eid=eid,
                        canonical_id=canonical_id,
                    )
                # 更新 mention_count
                count_eids = len(item.get("member_eids", [item.get("eid")]))
                session.run(
                    f"""
                    MATCH (ce:{canonical_type} {{canonical_id: $canonical_id}})
                    SET ce.mention_count = coalesce(ce.mention_count, 0) + $delta,
                        ce.updated_at = $now
                    """,
                    canonical_type=canonical_type,
                    canonical_id=canonical_id,
                    delta=count_eids,
                    now=now,
                )
            stats[0] += 1
            log.info("  [%d] SAME_AS → %s", idx, canonical_id[:20])

        elif action == "NEW":
            canonical_name = dec.get("canonical_name", item.get("representative_name") or item.get("name", ""))
            category_name = dec.get("category", "")
            category_desc = dec.get("category_description", "")
            canonical_id = str(uuid.uuid4())
            canonical_type = CANONICAL_MAP.get(label, "CanonicalEntity")

            is_new_category = bool(category_desc)

            if not dry_run:
                # 如果是新 Category，先创建
                if is_new_category:
                    session.run(
                        """
                        MERGE (c:Category {name: $name, mention_label: $mention_label})
                        ON CREATE SET c.description = $description,
                                      c.source = 'llm_generated',
                                      c.created_at = $created_at
                        """,
                        name=category_name,
                        mention_label=label,
                        description=category_desc,
                        created_at=now,
                    )
                    log.info("  [%d] New Category: %s", idx, category_name)
                    stats[2] += 1
                else:
                    stats[1] += 1

                # 创建 CanonicalEntity
                session.run(
                    f"""
                    MERGE (ce:{canonical_type} {{canonical_id: $canonical_id}})
                    ON CREATE SET ce.canonical_name = $canonical_name,
                                  ce.mention_count = 0,
                                  ce.created_at = $now
                    """,
                    canonical_id=canonical_id,
                    canonical_name=canonical_name,
                    now=now,
                )

                # RESOLVES_TO
                eids = item.get("member_eids", [item.get("eid")])
                for eid in eids:
                    session.run(
                        f"""
                        MATCH (m)
                        WHERE elementId(m) = $eid
                        MATCH (ce:{canonical_type} {{canonical_id: $canonical_id}})
                        MERGE (m)-[:RESOLVES_TO]->(ce)
                        """,
                        eid=eid,
                        canonical_id=canonical_id,
                    )

                # BELONGS_TO
                session.run(
                    f"""
                    MATCH (ce:{canonical_type} {{canonical_id: $canonical_id}})
                    MATCH (c:Category {{name: $category_name, mention_label: $mention_label}})
                    MERGE (ce)-[:BELONGS_TO]->(c)
                    """,
                    canonical_id=canonical_id,
                    category_name=category_name,
                    mention_label=label,
                )

                # 更新 mention_count
                count_eids = len(item.get("member_eids", [item.get("eid")]))
                session.run(
                    f"""
                    MATCH (ce:{canonical_type} {{canonical_id: $canonical_id}})
                    SET ce.mention_count = coalesce(ce.mention_count, 0) + $delta,
                        ce.updated_at = $now
                    """,
                    canonical_id=canonical_id,
                    delta=count_eids,
                    now=now,
                )

            log.info("  [%d] NEW → %s [%s%s]",
                     idx, canonical_name, category_name,
                     " (new category)" if is_new_category else "")

        else:
            log.warning("  [%d] Unknown action: %s", idx, action)

    return tuple(stats)


def clear_resolveto(session) -> int:
    """清除所有 RESOLVES_TO 和 BELONGS_TO 关系（用于 --force-reprocess）。"""
    r1 = session.run("MATCH ()-[r:RESOLVES_TO]->() DELETE r")
    r2 = session.run("MATCH ()-[r:BELONGS_TO]->() DELETE r")
    r3 = session.run("MATCH (ce) WHERE any(lbl IN labels(ce) WHERE lbl STARTS WITH 'Canonical') DETACH DELETE ce")
    s1 = r1.consume()
    s2 = r2.consume()
    s3 = r3.consume()
    total = s1.counters.relationships_deleted + s2.counters.relationships_deleted + s3.counters.nodes_deleted
    log.info("Cleared %d relations + %d Canonical nodes",
             s1.counters.relationships_deleted + s2.counters.relationships_deleted,
             s3.counters.nodes_deleted)
    return total


def main():
    parser = argparse.ArgumentParser(description="Gold Layer Incremental Processing")
    parser.add_argument("--silver-input", type=str, default=None,
                        help="Silver merge review JSON 路径")
    parser.add_argument("--batch-size", type=int, default=5, help="每批 Mention 数量")
    parser.add_argument("--uri", type=str, default=os.getenv("NEO4J_URI", "neo4j://localhost:7687"))
    parser.add_argument("--username", type=str, default=os.getenv("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", type=str, default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--database", type=str, default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--force-reprocess", action="store_true", help="清除已有映射，重新处理全部")
    parser.add_argument("--model", type=str, default="deepseek-v4-flash")
    parser.add_argument("--limit", type=int, default=None, help="限制处理批数（测试用）")
    args = parser.parse_args()

    if not args.password:
        raise ValueError("Neo4j password required. Pass --password or set NEO4J_PASSWORD")

    # 加载 config
    from pipeline.utils import load_config
    config = load_config()
    api_key = config.get("llm_key")
    if not api_key:
        raise ValueError("'llm_key' not found in script/config.json")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    # 初始化 Embedder
    embedder = Embedder()

    driver = GraphDatabase.driver(args.uri, auth=(args.username, args.password))
    with driver.session(database=args.database) as session:
        # Force reprocess
        if args.force_reprocess:
            clear_resolveto(session)

        # 加载全局状态
        all_categories = fetch_categories(session)
        total_cats = sum(len(v) for v in all_categories.values())
        log.info("Loaded %d Categories across %d labels", total_cats, len(all_categories))

        canonical_entities = fetch_canonical_entities(session)
        for label, ces in canonical_entities.items():
            log.info("  Canonical%s: %d entities", label, len(ces))

        # 构建工作队列
        queue = build_work_queue(session, args.silver_input)
        if not queue:
            log.info("No unresolved mentions. Done.")
            driver.close()
            return

        # 随机打乱（保证跨 Label 多样性）
        random.shuffle(queue)

        # 按 Label 分组，组内分批
        total_stats = [0, 0, 0, 0]  # [total, same_as, new_existing, new_newcat]
        batch_size = args.batch_size

        # Rich 进度条
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
        import logging as _logging
        _root = _logging.getLogger()
        _orig_level = _root.level
        _root.setLevel(_logging.WARNING)

        total_items = len(queue)
        total_batches = (total_items + batch_size - 1) // batch_size

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.fields[name]}"),
            TextColumn("  {task.fields[status]}"),
            TimeElapsedColumn(),
            refresh_per_second=4,
        ) as progress:
            summary_id = progress.add_task(
                "", total=total_batches,
                name="[bold green]Gold Incremental",
                status=f"0/{total_batches} batches",
            )
            batch_id = progress.add_task(
                "", total=total_items,
                name="[bold cyan]Processing",
                status="starting...",
            )

            q_idx = 0
            batch_count = 0
            while q_idx < len(queue):
                batch = queue[q_idx:q_idx + batch_size]
                q_idx += batch_size
                batch_count += 1

                # 准备 Top-5 候选
                batch_candidates(batch, canonical_entities, embedder)

                # 构造 prompt
                content = make_llm_prompt(batch, all_categories)
                progress.update(batch_id, status=f"LLM batch {batch_count}/{total_batches}")

                # LLM 裁决
                result = stream_llm_response(client, SYSTEM_PROMPT, content, args.model)
                if result is None:
                    progress.console.print(f"[red]Batch {batch_count} LLM failed[/red]")
                    continue

                decisions = result.get("decisions", [])
                if not decisions:
                    progress.console.print(f"[yellow]Batch {batch_count} returned empty decisions[/yellow]")
                    continue

                progress.update(batch_id, status=f"Writing batch {batch_count}/{total_batches}")

                # 执行决策
                stats = execute_decisions(session, batch, decisions, args.dry_run)
                for i in range(3):
                    total_stats[i + 1] += stats[i]
                total_stats[0] += len(batch)

                # 新创建的 CanonicalEntity 加入向量池
                new_ces = fetch_canonical_entities(session)
                for label, ces in new_ces.items():
                    existing = canonical_entities.get(label, [])
                    existing_ids = {ce["canonical_id"] for ce in existing}
                    existing_len = len(existing_ids)
                    for ce in ces:
                        if ce["canonical_id"] not in existing_ids:
                            ce["embedding"] = embedder.embed_single(ce["canonical_name"])
                            if not args.dry_run:
                                canonical_type = CANONICAL_MAP.get(label, "CanonicalEntity")
                                session.run(
                                    f"MATCH (ce:{canonical_type} {{canonical_id: $cid}}) "
                                    f"SET ce.embedding = $embedding",
                                    cid=ce["canonical_id"],
                                    embedding=ce["embedding"].tolist(),
                                )
                            existing.append(ce)
                    canonical_entities[label] = existing

                progress.update(
                    summary_id, completed=batch_count,
                    name="[bold green]Gold Incremental",
                    status=f"{batch_count}/{total_batches} batches | "
                           f"SAME={total_stats[1]} NEW+exist={total_stats[2]} NEW+cat={total_stats[3]}",
                )
                progress.update(
                    batch_id, completed=min(q_idx, total_items),
                    name="[bold cyan]Processing",
                )

                if args.limit and batch_count >= args.limit:
                    progress.console.print(f"[yellow]Limit reached ({args.limit} batches)[/yellow]")
                    break

        _root.setLevel(_orig_level)

    driver.close()

    log.info("=" * 60)
    log.info("Incremental processing complete:")
    log.info("  Items processed: %d", total_stats[0])
    log.info("  SAME_AS: %d", total_stats[1])
    log.info("  NEW + existing Category: %d", total_stats[2])
    log.info("  NEW + new Category: %d", total_stats[3])
    if args.dry_run:
        log.info("  [DRY-RUN] No data written to Neo4j")


if __name__ == "__main__":
    import random

    main()
