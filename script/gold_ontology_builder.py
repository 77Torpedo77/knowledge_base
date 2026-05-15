#!/usr/bin/env python3
"""
Gold Layer: LLM 全局本体推演（Ontology Building）
将每类 Mention 一次性喂给 DeepSeek，让其构建层级分类树。

输出格式：层级 JSON，包含 SAME_AS 等价合并和 IS_A 父子关系。
需要人工审查后才能导入 Neo4j。

用法:
  python gold_ontology_builder.py                           # 处理所有类别
  python gold_ontology_builder.py --label BaselineMention    # 只处理某一类
  python gold_ontology_builder.py --label BaselineMention --label MethodMention  # 多类别
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a rigorous academic domain ontology construction expert.

You will receive a JSON array of {category_name} entities extracted from ~200 research papers.
Each entity is identified by a short "idx" (integer). Fields:
  - "idx": unique index (use this in output "refs" to refer to this entity)
  - "name": the canonical name as used in the paper
  - "names": all observed variant names and aliases
  - "def": semantic definition

YOUR TASK: Build a rigorous, hierarchical academic ontology tree. Output a JSON object.

================ STRICT RULES ================

1. EQUIVALENCE (SAME_AS): If two entities are TRULY the exact same thing (just different naming conventions), merge them into one canonical entry. Put ALL their idx values into "refs".

   WARNING: "Similar" is NOT "equal"!
   - ResNet-50 and ResNet-101 are DIFFERENT entities (different architectures), do NOT merge.
   - DSO (Direct Sparse Odometry) and Stereo DSO are DIFFERENT entities, do NOT merge.
   - ORB-SLAM2 and ORB-SLAM3 are DIFFERENT entities, do NOT merge.
   - "Absolute Trajectory Error" and "Absolute Pose Error" are DIFFERENT metrics, do NOT merge.
   - "rotation error" and "translation error" are DIFFERENT metrics, do NOT merge.

   SAFE merges: entities that are clearly the same thing with different formatting:
   - "DSO" and "Direct Sparse Odometry" → SAME (it's the same algorithm)
   - "KITTI Odometry" and "KITTI dataset" → SAME (same benchmark)
   - "RMSE" and "Root Mean Square Error" → SAME (same metric)

2. HIERARCHY (IS_A): DO NOT merge a specific algorithm with a general paradigm!
   Instead, create parent-child relationships via "sub":
   - SimCLR IS_A Contrastive Learning (NOT same_as)
   - ORB-SLAM2 IS_A Feature-based SLAM (NOT same_as)
   - KITTI IS_A Autonomous Driving Benchmark (NOT same_as)

3. CANONICAL NAMING: Use the most widely recognized academic name as "name".
   Prefer full names over abbreviations (e.g., "ORB-SLAM2" not "ORBSLAM2").

4. NO FABRICATION: Only create parent categories based on clear evidence in the entity definitions.
   If entities don't naturally form a hierarchy, just list them at the top level.

5. COMPLETENESS: EVERY input idx MUST appear in exactly one "refs" array.
   Do not drop any entity.

================ OUTPUT SCHEMA (short keys to save tokens) ================

{
  "tree": [
    {
      "name": "canonical name string",
      "type": "Paradigm" | "Algorithm Family" | "Specific Algorithm" | "Metric Family" | "Specific Metric" | "Dataset Family" | "Specific Dataset" | "Task Category" | "Specific Task",
      "refs": [1, 2],
      "sub": [
        {
          "name": "...",
          "type": "...",
          "refs": [3],
          "sub": []
        }
      ]
    }
  ]
}

Key meanings:
  "tree" → the ontology tree root array
  "name" → canonical name for this node
  "type" → canonical type (see allowed values above)
  "refs" → indices of merged input entities (use the "idx" values from input)
  "sub"  → child nodes (always present, empty array [] for leaves)

Notes:
- "sub" is always present (can be empty array [])
- Entities with no clear hierarchical relationship go directly in the top-level "tree" array
- "type" should reflect the level: broader → "Paradigm"/"Algorithm Family", specific → "Specific Algorithm"/"Specific Metric"
"""

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "gold_output"
# 每批最大实体数（超过则自动分批）
BATCH_SIZE = 120


def _expand_output(node: dict, idx_map: dict[int, str]) -> dict:
    """将 LLM 短键名输出映射回完整结构。"""
    return {
        "canonical_name": node["name"],
        "canonical_type": node["type"],
        "mapped_mention_ids": [idx_map[i] for i in node.get("refs", [])],
        "children": [_expand_output(c, idx_map) for c in node.get("sub", [])],
    }


def build_ontology_for_label(client: OpenAI, label: str, entities: list[dict], model: str) -> dict:
    """调用 LLM 对一类 Mention 构建本体。"""
    idx_map: dict[int, str] = {}
    input_entities = []
    for i, e in enumerate(entities, 1):
        idx_map[i] = e["mention_id"]
        input_entities.append({
            "idx": i,
            "name": e["name"],
            "names": e["all_names"],
            "def": e["definition"],
        })

    category_names = {
        "TaskMention": "research task",
        "MethodMention": "proposed method/algorithm",
        "DatasetMention": "dataset/benchmark",
        "MetricMention": "evaluation metric",
        "BaselineMention": "baseline/comparison method",
        "FlawMention": "addressed existing flaw",
        "LimitationMention": "self-admitted limitation",
    }

    prompt = SYSTEM_PROMPT.replace("{category_name}", category_names.get(label, label))

    entities_json = json.dumps(input_entities, ensure_ascii=False)
    log.info("[%s] Sending %d entities to LLM (%d chars)...", label, len(entities), len(entities_json))

    max_retries = 2
    for attempt in range(1, max_retries + 1):
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Build the ontology for these {len(input_entities)} {category_names.get(label, label)} entities:\n\n{entities_json}"},
                ],
                response_format={"type": "json_object"},
                max_tokens=131072,
                reasoning_effort="max",
                extra_body={"thinking": {"type": "enabled"}},
                stream=True,
                timeout=900,
            )

            content_parts = []
            reasoning_parts = []
            usage = None

            for chunk in stream:
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = chunk.usage

                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    reasoning_parts.append(delta.reasoning_content)
                elif delta.content:
                    content_parts.append(delta.content)

            content = "".join(content_parts)
            reasoning_len = sum(len(s) for s in reasoning_parts)

            log.info("[%s] LLM done — reasoning: %d chars, output: %d chars, tokens: %s",
                     label, reasoning_len, len(content),
                     f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "?")

            if not content or not content.strip():
                log.error("[%s] LLM returned empty response", label)
                if attempt < max_retries:
                    log.info("[%s] Retrying (attempt %d/%d)...", label, attempt + 1, max_retries)
                    continue
                return None

            raw = json.loads(content)
            tree_key = "tree" if "tree" in raw else "ontology"
            if tree_key not in raw:
                log.error("[%s] LLM output missing 'tree' key", label)
                return None

            expanded = {
                "ontology": [_expand_output(n, idx_map) for n in raw[tree_key]],
            }
            return expanded

        except json.JSONDecodeError as e:
            log.error("[%s] JSON parse failed: %s", label, e)
            if attempt < max_retries:
                log.info("[%s] Retrying (attempt %d/%d)...", label, attempt + 1, max_retries)
                continue
            return None
        except Exception as e:
            log.error("[%s] API call failed (attempt %d/%d): %s", label, attempt, max_retries, e)
            if attempt < max_retries:
                import time as _t
                _t.sleep(5)
                continue
            return None

    return None


def validate_ontology(ontology_data: dict, entities: list[dict], label: str) -> bool:
    """校验本体输出的完整性。"""
    if "ontology" not in ontology_data:
        log.error("[%s] Missing 'ontology' key in output", label)
        return False

    input_ids = set(e["mention_id"] for e in entities)
    mapped_ids: set[str] = set()

    def collect_ids(nodes):
        for node in nodes:
            ids = node.get("mapped_mention_ids", [])
            for mid in ids:
                if mid in mapped_ids:
                    log.warning("[%s] Duplicate mention_id in ontology: %s", label, mid)
                mapped_ids.add(mid)
            collect_ids(node.get("children", []))

    collect_ids(ontology_data["ontology"])

    missing = input_ids - mapped_ids
    extra = mapped_ids - input_ids
    if missing:
        log.error("[%s] %d mention_ids missing from ontology: %s", label, len(missing), list(missing)[:5])
    if extra:
        log.error("[%s] %d unknown mention_ids in ontology: %s", label, len(extra), list(extra)[:5])

    if missing or extra:
        log.error("[%s] Validation FAILED: %d/%d covered", label, len(input_ids & mapped_ids), len(input_ids))
        return False

    log.info("[%s] Validation passed: all %d mention_ids covered", label, len(input_ids))
    return True


def main():
    parser = argparse.ArgumentParser(description="Gold Layer: LLM Ontology Building")
    parser.add_argument("--label", choices=[
        "TaskMention", "MethodMention", "DatasetMention",
        "MetricMention", "BaselineMention", "FlawMention", "LimitationMention",
    ], action="append", default=None, help="指定类别（可多次指定）")
    parser.add_argument("--input", default="D:/tools/knowledge_base/silver_input.json")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--model", default="deepseek-v4-flash")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 加载 config
    from pipeline.utils import load_config
    config = load_config()
    api_key = config.get("llm_key")
    if not api_key:
        raise ValueError("'llm_key' not found in config.json")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    # 加载输入
    with open(args.input, "r", encoding="utf-8") as f:
        input_data = json.load(f)

    labels = args.label or list(input_data.keys())

    for label in labels:
        entities = input_data.get(label, [])
        if not entities:
            log.warning("[%s] No entities to process", label)
            continue

        # 分批处理
        if len(entities) <= BATCH_SIZE:
            batches = [entities]
        else:
            batches = [entities[i:i + BATCH_SIZE] for i in range(0, len(entities), BATCH_SIZE)]
            log.info("[%s] Split into %d batches of ~%d", label, len(batches), BATCH_SIZE)

        all_ontologies = []
        for batch_idx, batch in enumerate(batches):
            log.info("[%s] Processing batch %d/%d (%d entities)", label, batch_idx + 1, len(batches), len(batch))
            ontology_data = build_ontology_for_label(client, f"{label}_batch{batch_idx + 1}", batch, args.model)
            if ontology_data is None:
                log.error("[%s] Batch %d failed, skipping", label, batch_idx + 1)
                continue
            all_ontologies.append(ontology_data)

        if not all_ontologies:
            log.error("[%s] All batches failed, skipping", label)
            continue

        # 合并批次
        if len(all_ontologies) == 1:
            merged = all_ontologies[0]
        else:
            merged = {"ontology": []}
            for ont in all_ontologies:
                merged["ontology"].extend(ont.get("ontology", []))

        # 校验
        valid = validate_ontology(merged, entities, label)

        # 保存（无论校验是否通过，都保存供人工审查）
        out_path = OUTPUT_DIR / f"{label}_ontology.json"
        out_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("[%s] Saved to %s (valid=%s)", label, out_path, valid)

        # 同时保存一个可读的树状文本
        tree_path = OUTPUT_DIR / f"{label}_tree.txt"
        with tree_path.open("w", encoding="utf-8") as f:
            _print_tree(merged["ontology"], f)
        log.info("[%s] Tree view saved to %s", label, tree_path)

    log.info("=" * 60)
    log.info("All done. Review the JSON files in %s before importing to Neo4j.", OUTPUT_DIR)


def _print_tree(nodes, f, indent=0):
    """将本体树输出为可读的缩进文本。"""
    for node in nodes:
        name = node.get("canonical_name", "?")
        typ = node.get("canonical_type", "")
        ids = node.get("mapped_mention_ids", [])
        f.write(f"{'  ' * indent}{name} [{typ}] ({len(ids)} mentions)\n")
        _print_tree(node.get("children", []), f, indent + 1)


if __name__ == "__main__":
    main()
