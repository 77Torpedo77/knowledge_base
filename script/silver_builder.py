#!/usr/bin/env python3
"""
Silver Layer Builder — 确定性规则清洗 + 别名对齐
从 Neo4j Bronze Layer 读取 *Mention 节点，通过两条确定性规则归并等价实体：
  Rule 1: 大小写/标点/空格归一化后完全相同
  Rule 2: 别名交叉匹配（过滤泛称别名）
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

# 泛称别名黑名单：这些词在不同论文中指代不同方法，不能用于跨实体对齐
GENERIC_ALIASES = {
    # 自称类（不同论文指代不同方法）
    "proposed method", "proposed system", "proposed approach", "proposed framework",
    "proposed algorithm", "proposed model", "proposed architecture",
    "our method", "our system", "our approach", "our framework", "our algorithm",
    "our model", "our method", "our strategy",
    "the proposed method", "the proposed system", "the proposed approach",
    "the proposed framework", "the proposed algorithm", "the proposed model",
    "the proposed strategy",
    "baseline", "our baseline", "the baseline",
    "ours", "proposed", "this method", "this work",
    # 过于宽泛的学术术语
    "deep learning", "machine learning", "neural network", "cnn", "rnn",
    # 过于宽泛的指标/描述词汇
    "accuracy", "error", "drift", "loss", "score", "metric", "metrics",
    "accuracy metrics", "performance", "efficiency", "robustness",
    "runtime", "time", "speed", "latency", "throughput",
    "precision", "recall", "f1",
}

# 别名归一化后的最小长度：过短的别名（如 "δ1"、"δ2"、"δ3"）不可靠
MIN_ALIAS_NORM_LEN = 3


def normalize(text: str) -> str:
    """大小写 + 标点 + 空格归一化。"""
    text = text.lower().strip()
    # 去除标点（保留字母数字）
    text = re.sub(r"[^a-z0-9+]", "", text)
    return text


def is_generic_alias(alias: str) -> bool:
    """判断别名是否为泛称（不能用于跨实体对齐）。"""
    norm = normalize(alias)
    if len(norm) < MIN_ALIAS_NORM_LEN:
        return True
    return norm in {normalize(a) for a in GENERIC_ALIASES}


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
    对同类 Mention 执行确定性归并，返回等价组列表。
    每组包含 2+ 个等价 Mention dict。
    """
    uf = UnionFind()
    name_to_mentions: dict[str, list[dict]] = defaultdict(list)

    # 注册所有节点
    for m in mentions:
        uf.find(m["eid"])
        name_to_mentions[m["name"].lower()].append(m)

    # Rule 1: 归一化后完全相同
    norm_groups: dict[str, list[dict]] = defaultdict(list)
    for m in mentions:
        norm = normalize(m["name"])
        if not norm:
            continue
        norm_groups[norm].append(m)

    rule1_count = 0
    for norm, group in norm_groups.items():
        if len(group) > 1:
            for i in range(1, len(group)):
                uf.union(group[0]["eid"], group[i]["eid"])
            rule1_count += 1

    # Rule 2: 别名交叉匹配
    # 构建别名→eid映射（排除泛称）
    alias_index: dict[str, list[str]] = defaultdict(list)
    name_lower_index: dict[str, str] = {}  # name.lower() -> eid (representative)

    for m in mentions:
        nl = m["name"].lower()
        if nl not in name_lower_index:
            name_lower_index[nl] = m["eid"]
        for alias in m["aliases"]:
            if not is_generic_alias(alias):
                alias_index[normalize(alias)].append(m["eid"])

    rule2_count = 0
    for m in mentions:
        mn = normalize(m["name"])
        # 别名中是否包含其他实体的 name？
        if mn in alias_index:
            for other_eid in alias_index[mn]:
                if other_eid != m["eid"]:
                    uf.union(m["eid"], other_eid)
                    rule2_count += 1

        # 该实体的别名是否与其他实体的 name 匹配？
        for alias in m["aliases"]:
            if is_generic_alias(alias):
                continue
            an = normalize(alias)
            if an in name_lower_index:
                other_eid = name_lower_index[an]
                if other_eid != m["eid"]:
                    uf.union(m["eid"], other_eid)
                    rule2_count += 1

    # 提取等价组（每组 > 1 个成员）
    groups = uf.groups()
    result = []
    for root, members in groups.items():
        if len(members) > 1:
            group_mentions = [m for m in mentions if m["eid"] in members]
            if len(group_mentions) > 1:
                result.append(group_mentions)

    log.info("  Rule1 (normalization): %d groups, Rule2 (alias): %d merges, total groups: %d",
             rule1_count, rule2_count, len(result))
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
