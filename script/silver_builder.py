#!/usr/bin/env python3
"""
Silver Layer Builder — 保守确定性清洗
从 Neo4j Bronze Layer 读取 *Mention 节点，仅通过 name 的保守归一化归并等价实体：
  Rule 1: 大小写/空格/连接符/下划线归一化后完全相同
归并结果写入 Neo4j SAME_AS 关系。

用法:
  python silver_builder.py --dry-run                    # 预览
  python silver_builder.py                              # 执行
  python silver_builder.py --label BaselineMention       # 只处理某一类
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata
from collections import defaultdict
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

GENERIC_NAME_KEYS = {
    "selfcollecteddataset",
    "selfcollecteddata",
    "ourowndataset",
    "ourowndata",
    "ourdataset",
    "customdataset",
    "privatedataset",
}


def normalize(text: str) -> str:
    """保守归一化：仅忽略大小写、空白、连字符和下划线等排版差异。"""
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    # 保留 *、#、希腊字母、比较符等可能改变语义的符号。
    text = re.sub(r"[\s\-_]+", "", text)
    return text


def is_generic_name(text: str) -> bool:
    """描述性泛称不作为跨论文 SAME_AS 证据。"""
    return normalize(text) in GENERIC_NAME_KEYS


class UnionFind:
    """并查集，用于分组等价实体。"""

    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self.parent:
            self.parent[x] = x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            result[self.find(x)].append(x)
        return dict(result)


def fetch_mentions(session, label: str) -> list[dict]:
    """从 Neo4j 读取某类 Mention 的全部节点。"""
    result = session.run(f"""
        MATCH (m:{label})
        RETURN elementId(m) AS eid, m.name AS name, m.aliases AS aliases, m.semantic_definition AS def
    """)
    mentions = []
    for row in result:
        mentions.append({
            "eid": row["eid"],
            "name": row["name"],
            "aliases": row["aliases"] or [],
            "definition": row["def"] or "",
        })
    return mentions


def silver_deduplicate(mentions: list[dict]) -> list[list[dict]]:
    """
    对同类 Mention 执行保守确定性归并，返回等价组列表。
    每组包含 2+ 个等价 Mention dict。
    """
    uf = UnionFind()

    # 注册所有节点
    for m in mentions:
        uf.find(m["eid"])

    # 仅基于 name 的保守归一化合并，不使用 aliases 作为 SAME_AS 证据。
    norm_groups: dict[str, list[dict]] = defaultdict(list)
    for m in mentions:
        if is_generic_name(m["name"]):
            continue
        norm = normalize(m["name"])
        if not norm:
            continue
        norm_groups[norm].append(m)

    name_match_count = 0
    for norm, group in norm_groups.items():
        if len(group) > 1:
            for i in range(1, len(group)):
                uf.union(group[0]["eid"], group[i]["eid"])
            name_match_count += 1

    # 提取等价组（每组 > 1 个成员）
    groups = uf.groups()
    result = []
    for root, members in groups.items():
        if len(members) > 1:
            group_mentions = [m for m in mentions if m["eid"] in members]
            if len(group_mentions) > 1:
                result.append(group_mentions)

    log.info("  Conservative name normalization: %d groups, total groups: %d",
             name_match_count, len(result))
    return result


def pick_canonical(group: list[dict]) -> dict:
    """从等价组中选出代表节点：优先选 name 最短且有定义的。"""
    # 优先选 name 包含完整名称（有括号/缩写展开）的
    with_paren = [m for m in group if "(" in m["name"]]
    if with_paren:
        return max(with_paren, key=lambda m: len(m["name"]))

    # 否则选 name 最长的
    return max(group, key=lambda m: len(m["name"]))


def write_same_as(session, groups: list[list[dict]], label: str) -> int:
    """将 SAME_AS 关系写入 Neo4j。"""
    count = 0
    for group in groups:
        canonical = pick_canonical(group)
        for m in group:
            if m["eid"] == canonical["eid"]:
                continue
            session.run("""
                MATCH (a:%(label)s), (b:%(label)s)
                WHERE elementId(a) = $eid_a AND elementId(b) = $eid_b
                MERGE (a)-[:SAME_AS]->(b)
                MERGE (b)-[:SAME_AS]->(a)
            """ % {"label": label}, eid_a=m["eid"], eid_b=canonical["eid"])
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Silver Layer: 确定性规则归并 Mention 节点")
    parser.add_argument("--uri", default="neo4j://127.0.0.1:7687")
    parser.add_argument("--username", default="neo4j")
    parser.add_argument("--password", default="12345678")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--dry-run", action="store_true", help="只打印归并结果，不写入 Neo4j")
    parser.add_argument("--label", choices=MENTION_LABELS, default=None,
                        help="只处理指定的 Mention 类型")
    parser.add_argument("--export", type=str, default=None,
                        help="将 Silver 结果导出为 JSON 文件（用于 Gold Layer）")
    args = parser.parse_args()

    labels = [args.label] if args.label else MENTION_LABELS

    driver = GraphDatabase.driver(args.uri, auth=(args.username, args.password))

    total_groups = 0
    total_relations = 0
    silver_stats = {}  # label -> {total, merged, remaining}

    with driver.session(database=args.database) as session:
        for label in labels:
            mentions = fetch_mentions(session, label)
            log.info("[%s] %d mentions loaded", label, len(mentions))

            groups = silver_deduplicate(mentions)

            if args.dry_run:
                log.info("[%s] DRY RUN: %d merge groups", label, len(groups))
                for group in groups:
                    canonical = pick_canonical(group)
                    others = [m["name"] for m in group if m["eid"] != canonical["eid"]]
                    log.info("  ✓ \"%s\" ← %s", canonical["name"], others)
            else:
                n = write_same_as(session, groups, label)
                log.info("[%s] Wrote %d SAME_AS relations (%d groups)", label, n, len(groups))
                total_relations += n

            merged_count = sum(len(g) for g in groups) - len(groups)  # reduced
            silver_stats[label] = {
                "total": len(mentions),
                "merged_away": merged_count,
                "remaining": len(mentions) - merged_count,
                "groups": len(groups),
            }
            total_groups += len(groups)

    driver.close()

    log.info("=" * 60)
    log.info("Silver Layer summary:")
    for label, stats in silver_stats.items():
        log.info("  %s: %d → %d (%d merged in %d groups)",
                 label, stats["total"], stats["remaining"],
                 stats["merged_away"], stats["groups"])
    log.info("Total: %d merge groups, %d SAME_AS relations %s",
             total_groups, total_relations,
             "(dry-run)" if args.dry_run else "")

    if args.export:
        # 导出 Silver 合并结果：仅包含被合并的条目
        export_data = {}
        driver = GraphDatabase.driver(args.uri, auth=(args.username, args.password))
        with driver.session(database=args.database) as session:
            for label in labels:
                mentions = fetch_mentions(session, label)
                groups = silver_deduplicate(mentions)

                # 构建 eid -> group_id 映射
                eid_to_group: dict[str, int] = {}
                for gid, group in enumerate(groups):
                    for m in group:
                        eid_to_group[m["eid"]] = gid

                # 只导出被合并的 mention（按组输出）
                merged_mentions = []
                for gid, group in enumerate(groups):
                    if len(group) < 2:
                        continue
                    merged_mentions.append({
                        "group_id": gid,
                        "members": [
                            {
                                "mention_id": m["eid"],
                                "name": m["name"],
                                "aliases": m["aliases"],
                            }
                            for m in group
                        ],
                    })

                if merged_mentions:
                    export_data[label] = {
                        "stats": silver_stats[label],
                        "groups": merged_mentions,
                    }

        driver.close()

        out_path = Path(args.export)
        out_path.write_text(json.dumps(export_data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Silver data exported to %s", out_path)


if __name__ == "__main__":
    main()
