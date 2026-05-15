#!/usr/bin/env python3
"""
Bronze Layer 导出：将 Bronze Mention 按 SAME_AS 分组导出为 Silver 层输入 JSON。
每组 SAME_AS 取一个代表（canonical），附带同组的原始 name 列表。

用法:
  python bronze_export.py                          # 导出所有类别
  python bronze_export.py --label BaselineMention   # 只导出某一类
  python bronze_export.py --output silver_input.json
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


def export_label(session, label: str) -> list[dict]:
    """从 Neo4j 导出某类 Mention，按 SAME_AS 分组后输出。"""
    # 获取所有该类 Mention
    result = session.run(f"""
        MATCH (m:{label})
        OPTIONAL MATCH (m)-[:SAME_AS]-(peer:{label})
        WITH m, collect(DISTINCT peer.name) AS peer_names
        RETURN elementId(m) AS eid, m.name AS name, m.aliases AS aliases,
               m.semantic_definition AS def, peer_names
    """)

    # 按 SAME_AS 组聚合（用 BFS 找连通分量）
    mentions = []
    for row in result:
        mentions.append({
            "eid": row["eid"],
            "name": row["name"],
            "aliases": row["aliases"] or [],
            "definition": row["def"] or "",
            "peer_names": [n for n in (row["peer_names"] or []) if n != row["name"]],
        })

    # BFS 找 SAME_AS 连通分量
    eid_to_idx = {m["eid"]: i for i, m in enumerate(mentions)}
    adj: dict[int, set[int]] = {i: set() for i in range(len(mentions))}
    for i, m in enumerate(mentions):
        for peer_name in m["peer_names"]:
            for j, other in enumerate(mentions):
                if other["name"] == peer_name and j != i:
                    adj[i].add(j)
                    adj[j].add(i)

    visited = set()
    groups: list[list[int]] = []
    for i in range(len(mentions)):
        if i in visited:
            continue
        group = []
        stack = [i]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            group.append(node)
            for neighbor in adj[node]:
                if neighbor not in visited:
                    stack.append(neighbor)
        groups.append(group)

    # 每组输出一个实体
    entities = []
    for group_indices in groups:
        group_mentions = [mentions[i] for i in group_indices]
        # 选代表：优先选有完整名称（含括号）且定义最长的
        representative = max(group_mentions, key=lambda m: (len(m["definition"]), len(m["name"])))

        all_names = sorted(set(m["name"] for m in group_mentions) | set(a for m in group_mentions for a in m["aliases"]))

        entities.append({
            "mention_id": representative["eid"],
            "name": representative["name"],
            "all_names": all_names,
            "aliases": representative["aliases"],
            "definition": representative["definition"],
        })

    # 按 mention_id 排序保证稳定
    entities.sort(key=lambda e: e["mention_id"])
    return entities


def main():
    parser = argparse.ArgumentParser(description="Gold Layer: 导出 Mention 为 LLM 友好的 JSON")
    parser.add_argument("--uri", default="neo4j://127.0.0.1:7687")
    parser.add_argument("--username", default="neo4j")
    parser.add_argument("--password", default="12345678")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--label", choices=MENTION_LABELS, default=None,
                        help="只导出指定类别")
    parser.add_argument("--output", default="D:/tools/knowledge_base/silver_input.json",
                        help="输出 JSON 文件路径")
    args = parser.parse_args()

    labels = [args.label] if args.label else MENTION_LABELS

    driver = GraphDatabase.driver(args.uri, auth=(args.username, args.password))
    output = {}

    with driver.session(database=args.database) as session:
        for label in labels:
            entities = export_label(session, label)
            output[label] = entities
            log.info("[%s] %d entities exported (after Silver dedup)", label, len(entities))

    driver.close()

    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Exported to %s", args.output)

    # 统计 token 估算
    total_chars = sum(
        len(e["name"]) + len(e["definition"]) + sum(len(n) for n in e["all_names"])
        for entities in output.values()
        for e in entities
    )
    log.info("Estimated ~%d tokens (%d chars)", total_chars // 3, total_chars)


if __name__ == "__main__":
    main()
