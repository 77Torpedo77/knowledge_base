#!/usr/bin/env python3
"""
Gold Layer Importer: 将审查后的 Ontology JSON 导入 Neo4j

从 gold_output/{Label}_ontology.json 读取本体树，创建：
  - CanonicalEntity 节点（金层规范实体）
  - IS_A 关系（父子层级）
  - RESOLVES_TO 关系（Bronze Mention → Gold CanonicalEntity）

用法:
  python gold_importer.py --dry-run                     # 预览
  python gold_importer.py                               # 全量导入
  python gold_importer.py --label BaselineMention        # 只导入某类
  python gold_importer.py --output-dir D:/path/to/gold   # 指定输入目录
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from neo4j import GraphDatabase

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

# CanonicalEntity 的 label 映射
CANONICAL_LABELS = {
    "TaskMention": "CanonicalTask",
    "MethodMention": "CanonicalMethod",
    "DatasetMention": "CanonicalDataset",
    "MetricMention": "CanonicalMetric",
    "BaselineMention": "CanonicalBaseline",
    "FlawMention": "CanonicalFlaw",
    "LimitationMention": "CanonicalLimitation",
}


def collect_nodes_flat(ontology: list[dict], parent_id: str | None = None) -> list[tuple[dict, str | None]]:
    """递归展平本体树，返回 [(node, parent_canonical_id), ...]。"""
    result = []
    for node in ontology:
        canonical_id = node["canonical_id"]
        result.append((node, parent_id))
        result.extend(collect_nodes_flat(node.get("children", []), canonical_id))
    return result


def assign_ids(ontology: list[dict], prefix: str) -> None:
    """为每个节点分配唯一的 canonical_id（如果没有的话）。"""
    counter = [0]

    def _assign(nodes):
        for node in nodes:
            counter[0] += 1
            node["canonical_id"] = f"{prefix}_{counter[0]}"
            _assign(node.get("children", []))

    _assign(ontology)


def import_label(session, label: str, ontology: list[dict], *, dry_run: bool = False) -> int:
    """将某类的本体树导入 Neo4j。"""
    canon_label = CANONICAL_LABELS.get(label, "CanonicalEntity")

    # 分配 ID
    assign_ids(ontology, label)
    flat = collect_nodes_flat(ontology)

    if dry_run:
        log.info("[%s] DRY RUN: %d canonical nodes to create", label, len(flat))
        for node, parent_id in flat[:5]:
            log.info("  %s [%s] → %s mentions, parent=%s",
                     node["canonical_name"], node.get("canonical_type", ""),
                     len(node.get("mapped_mention_ids", [])),
                     parent_id or "TOP")
        if len(flat) > 5:
            log.info("  ... and %d more", len(flat) - 5)
        return len(flat)

    # 清理旧的该类 CanonicalEntity
    session.run(f"MATCH (c:{canon_label}) DETACH DELETE c").consume()
    # 清理旧的 RESOLVES_TO
    session.run(f"""
        MATCH (m:{label})-[r:RESOLVES_TO]->()
        DELETE r
    """).consume()

    created = 0
    # 创建 CanonicalEntity 节点
    for node, parent_id in flat:
        mention_ids = node.get("mapped_mention_ids", [])
        session.run(f"""
            CREATE (c:{canon_label} {{
                canonical_id: $cid,
                canonical_name: $name,
                canonical_type: $ctype,
                mention_count: $mcnt
            }})
        """, cid=node["canonical_id"], name=node["canonical_name"],
             ctype=node.get("canonical_type", ""), mcnt=len(mention_ids)).consume()
        created += 1

    # 创建 IS_A 关系
    for node, parent_id in flat:
        if parent_id:
            session.run(f"""
                MATCH (c:{canon_label} {{canonical_id: $cid}})
                MATCH (p:{canon_label} {{canonical_id: $pid}})
                CREATE (c)-[:IS_A]->(p)
            """, cid=node["canonical_id"], pid=parent_id).consume()

    # 创建 RESOLVES_TO 关系（Mention → CanonicalEntity）
    resolved = 0
    for node, _ in flat:
        mention_ids = node.get("mapped_mention_ids", [])
        for mid in mention_ids:
            session.run(f"""
                MATCH (m:{label})
                WHERE elementId(m) = $eid
                MATCH (c:{canon_label} {{canonical_id: $cid}})
                CREATE (m)-[:RESOLVES_TO]->(c)
            """, eid=mid, cid=node["canonical_id"]).consume()
            resolved += 1

    log.info("[%s] Created %d %s nodes, %d IS_A relations, %d RESOLVES_TO relations",
             label, created, canon_label, created - 1, resolved)
    return created


def main():
    parser = argparse.ArgumentParser(description="Gold Layer: Import Ontology JSON to Neo4j")
    parser.add_argument("--uri", default="neo4j://127.0.0.1:7687")
    parser.add_argument("--username", default="neo4j")
    parser.add_argument("--password", default="12345678")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--label", choices=MENTION_LABELS, default=None,
                        help="只导入指定类别")
    parser.add_argument("--output-dir", default="D:/tools/knowledge_base/gold_output",
                        help="gold_ontology_builder 的输出目录")
    args = parser.parse_args()

    labels = [args.label] if args.label else MENTION_LABELS
    output_dir = Path(args.output_dir)

    driver = GraphDatabase.driver(args.uri, auth=(args.username, args.password))

    with driver.session(database=args.database) as session:
        # 初始化 CanonicalEntity 唯一约束
        for label in labels:
            canon_label = CANONICAL_LABELS.get(label, "CanonicalEntity")
            session.run(f"""
                CREATE CONSTRAINT {canon_label.lower()}_id_unique IF NOT EXISTS
                FOR (c:{canon_label})
                REQUIRE c.canonical_id IS UNIQUE
            """).consume()

        total = 0
        for label in labels:
            ontology_path = output_dir / f"{label}_ontology.json"
            if not ontology_path.exists():
                log.warning("[%s] No ontology file found at %s, skipping", label, ontology_path)
                continue

            with ontology_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            ontology = data.get("ontology", [])
            if not ontology:
                log.warning("[%s] Empty ontology, skipping", label)
                continue

            n = import_label(session, label, ontology, dry_run=args.dry_run)
            total += n

    driver.close()
    log.info("=" * 60)
    log.info("Gold Layer import done: %d canonical nodes created %s", total,
             "(dry-run)" if args.dry_run else "")


if __name__ == "__main__":
    main()
