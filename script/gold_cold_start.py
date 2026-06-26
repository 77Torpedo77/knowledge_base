#!/usr/bin/env python3
"""
Gold Layer 冷启动：按 Mention 类型独立生成 Category 列表。

对每种 Label（TaskMention / MethodMention / ...），分别采样喂给 LLM，
生成该类型专属的 Category 分类体系，写入 Category 节点。

用法:
  python gold_cold_start.py --dry-run                     # 预览
  python gold_cold_start.py                               # 执行
  python gold_cold_start.py --sample-size 30               # 自定义采样数
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
from datetime import datetime, timezone

from neo4j import GraphDatabase
from openai import OpenAI

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

# 每种 Label 的 Category 生成 Prompt
CATEGORY_PROMPTS = {
    "TaskMention": """\
You are an expert in SLAM, visual odometry, and 3D computer vision.

I will show you a list of RESEARCH TASKS extracted from ~200 papers.
Each is a specific research problem or task addressed by a paper.

YOUR TASK: Propose macro-level categories that group similar research tasks.
Examples: "Visual Odometry", "Depth Estimation", "Visual-Inertial SLAM", "Place Recognition", "Sensor Calibration", "3D Reconstruction"

OUTPUT ONLY VALID JSON:
{"categories": [{"name": "...", "description": "..."}, ...]}
""",
    "MethodMention": """\
You are an expert in SLAM, visual odometry, and 3D computer vision.

I will show you a list of PROPOSED METHODS/ALGORITHMS extracted from ~200 papers.
Each is a specific algorithm, model, or technical module proposed by a paper.

YOUR TASK: Propose macro-level categories that group similar methods.
Examples: "SLAM Systems", "Visual Odometry Methods", "Feature Matching", "NeRF-based Methods", "Gaussian Splatting", "Uncertainty Estimation"

OUTPUT ONLY VALID JSON:
{"categories": [{"name": "...", "description": "..."}, ...]}
""",
    "BaselineMention": """\
You are an expert in SLAM, visual odometry, and 3D computer vision.

I will show you a list of BASELINE/COMPARISON METHODS extracted from ~200 papers.
Each is an existing method that a paper compared against.

YOUR TASK: Propose macro-level categories that group similar baseline methods.
Examples: "SLAM Systems", "Visual Odometry Methods", "Feature Matching", "Depth Estimation Methods", "Deep Learning Baselines", "Classical Methods"

OUTPUT ONLY VALID JSON:
{"categories": [{"name": "...", "description": "..."}, ...]}
""",
    "DatasetMention": """\
You are an expert in SLAM, visual odometry, and 3D computer vision.

I will show you a list of DATASETS/BENCHMARKS extracted from ~200 papers.
Each is a dataset used for training or evaluation.

YOUR TASK: Propose macro-level categories that group similar datasets.
Examples: "Autonomous Driving Benchmarks", "Indoor RGB-D Datasets", "Visual-Inertial Datasets", "Drone/Aerial Datasets", "Synthetic Datasets", "Depth Estimation Datasets"

OUTPUT ONLY VALID JSON:
{"categories": [{"name": "...", "description": "..."}, ...]}
""",
    "MetricMention": """\
You are an expert in SLAM, visual odometry, and 3D computer vision.

I will show you a list of EVALUATION METRICS extracted from ~200 papers.
Each is a quantitative metric used to measure performance.

YOUR TASK: Propose macro-level categories that group similar metrics.
Examples: "Trajectory Accuracy Metrics", "Depth Accuracy Metrics", "Runtime/Complexity Metrics", "Uncertainty Metrics", "Pose Estimation Metrics"

OUTPUT ONLY VALID JSON:
{"categories": [{"name": "...", "description": "..."}, ...]}
""",
    "FlawMention": """\
You are an expert in SLAM, visual odometry, and 3D computer vision.

I will show you a list of EXISTING FLAWS/PROBLEMS extracted from ~200 papers.
Each is a technical limitation or failure mode of prior work that a paper aims to address.

YOUR TASK: Propose macro-level categories that group similar flaws.
Examples: "Robustness Issues", "Feature Dependency Problems", "Computational Efficiency", "Sensor Limitations", "Assumption Violations"

OUTPUT ONLY VALID JSON:
{"categories": [{"name": "...", "description": "..."}, ...]}
""",
    "LimitationMention": """\
You are an expert in SLAM, visual odometry, and 3D computer vision.

I will show you a list of SELF-ADMITTED LIMITATIONS extracted from ~200 papers.
Each is a weakness that the authors acknowledge about their own method.

YOUR TASK: Propose macro-level categories that group similar limitations.
Examples: "Lack of Real-World Validation", "Computational Cost", "Sensitivity to Hyperparameters", "Limited Generalization", "Sensor Dependency"

OUTPUT ONLY VALID JSON:
{"categories": [{"name": "...", "description": "..."}, ...]}
""",
}


def create_schema(session) -> None:
    """创建 Gold 层 Schema 约束。"""
    constraints = [
        "CREATE CONSTRAINT category_name_label_unique IF NOT EXISTS "
        "FOR (c:Category) REQUIRE (c.name, c.mention_label) IS NODE KEY",
    ]
    for label in MENTION_LABELS:
        canonical = CANONICAL_MAP[label]
        constraints.append(
            f"CREATE CONSTRAINT {canonical.lower()}_id_unique IF NOT EXISTS "
            f"FOR (ce:{canonical}) REQUIRE ce.canonical_id IS UNIQUE"
        )

    for stmt in constraints:
        try:
            session.run(stmt)
        except Exception as e:
            log.warning("Constraint may already exist: %s", e)
    log.info("Schema initialized")


def sample_mentions(session, label: str, sample_size: int) -> list[dict]:
    """对指定 Label 随机采样 Mention。"""
    result = session.run(
        f"MATCH (m:{label}) RETURN m.name AS name, m.semantic_definition AS definition "
        f"ORDER BY rand() LIMIT $limit",
        limit=sample_size,
    )
    rows = [{"name": r["name"], "definition": r["definition"] or ""} for r in result]
    log.info("[%s] Sampled %d / requested %d", label, len(rows), sample_size)
    return rows


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
            log.info(
                "  LLM done — output: %d chars, tokens: %s",
                len(response_text),
                f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "?",
            )

            if not response_text.strip():
                log.error("  LLM returned empty response (attempt %d/%d)", attempt, max_retries)
                continue

            return json.loads(response_text)

        except json.JSONDecodeError as e:
            log.error("  JSON parse failed (attempt %d/%d): %s", attempt, max_retries, e)
            continue
        except Exception as e:
            log.error("  API call failed (attempt %d/%d): %s", attempt, max_retries, e)
            import time as _t
            _t.sleep(5)
            continue

    return None


def write_categories(session, mention_label: str, categories: list[dict], dry_run: bool = False) -> int:
    """写入 Category 节点（按 mention_label 区分）。"""
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for cat in categories:
        name = cat.get("name", "").strip()
        description = cat.get("description", "").strip()
        if not name:
            continue
        if dry_run:
            log.info("  [DRY-RUN] [%s] %s — %s", mention_label, name, description[:60])
            written += 1
            continue
        session.run(
            """
            MERGE (c:Category {name: $name, mention_label: $mention_label})
            ON CREATE SET c.description = $description,
                          c.source = 'llm_generated',
                          c.created_at = $created_at
            """,
            name=name,
            mention_label=mention_label,
            description=description,
            created_at=now,
        )
        written += 1
        log.info("  [%s] Created: %s", mention_label, name)
    return written


def clear_all_categories(session) -> int:
    """清除所有旧 Category。"""
    r = session.run("MATCH (c:Category) DETACH DELETE c")
    count = r.consume().counters.nodes_deleted
    log.info("Cleared %d old Category nodes", count)
    return count


def main():
    parser = argparse.ArgumentParser(description="Gold Layer Cold Start: generate Categories per Label")
    parser.add_argument("--sample-size", type=int, default=50, help="每种 Mention 采样数量")
    parser.add_argument("--labels", nargs="*", default=None,
                        help="指定 Label（默认全部 7 种）")
    parser.add_argument("--uri", type=str, default=os.getenv("NEO4J_URI", "neo4j://localhost:7687"))
    parser.add_argument("--username", type=str, default=os.getenv("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", type=str, default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--database", type=str, default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--model", type=str, default="deepseek-v4-flash")
    args = parser.parse_args()

    if not args.password:
        raise ValueError("Neo4j password required. Pass --password or set NEO4J_PASSWORD")

    from pipeline.utils import load_config
    config = load_config()
    api_key = config.get("llm_key")
    if not api_key:
        raise ValueError("'llm_key' not found in script/config.json")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    labels = args.labels or MENTION_LABELS

    driver = GraphDatabase.driver(args.uri, auth=(args.username, args.password))
    with driver.session(database=args.database) as session:
        # Schema
        create_schema(session)

        # 清除旧 Category
        if not args.dry_run:
            clear_all_categories(session)

        total_categories = 0

        for label in labels:
            samples = sample_mentions(session, label, args.sample_size)
            if not samples:
                log.warning("[%s] No mentions to sample, skipping", label)
                continue

            prompt = CATEGORY_PROMPTS.get(label, CATEGORY_PROMPTS["MethodMention"])
            content_lines = [f"The following {label} entities were extracted from SLAM/visual odometry papers:"]
            for s in samples:
                content_lines.append(f"  - {s['name']}: {s['definition'][:200]}")

            content = "\n".join(content_lines)
            log.info("[%s] Sending %d mentions to LLM...", label, len(samples))

            result = stream_llm_response(client, prompt, content, args.model)
            if result is None:
                log.error("[%s] LLM failed, skipping", label)
                continue

            categories = result.get("categories", [])
            log.info("[%s] LLM proposed %d categories", label, len(categories))

            written = write_categories(session, label, categories, args.dry_run)
            total_categories += written

    driver.close()

    log.info("=" * 60)
    log.info("Cold start complete: %d categories across %d labels",
             total_categories, len(labels))
    if args.dry_run:
        log.info("  [DRY-RUN] No data written to Neo4j")


if __name__ == "__main__":
    main()
