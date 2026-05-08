"""测试 LLM 提取稳定性：对同一篇论文运行 3 次，保存并对比原始输出"""
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from pipeline.chunker import chunk_markdown
from pipeline.extractor import llm_extract
from pipeline.utils import load_config

from openai import OpenAI

DATA_DIR = SCRIPT_DIR.parent / "zotero_data"
CITE_KEY = "pan2025RobustDirect"
N_RUNS = 3
OUTPUT_DIR = SCRIPT_DIR.parent / "pipeline_output" / "stability_test"


def main():
    md_path = DATA_DIR / CITE_KEY / "full_clear_table.md"
    text = md_path.read_text(encoding="utf-8")
    blocks = chunk_markdown(text)
    print(f"Loaded {len(blocks)} blocks from {CITE_KEY}")

    config = load_config()
    client = OpenAI(api_key=config["llm_key"], base_url="https://api.deepseek.com")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for i in range(1, N_RUNS + 1):
        print(f"\n{'='*50}")
        print(f"Run {i}/{N_RUNS}")
        result = llm_extract(client, blocks)
        if result is None:
            print(f"  FAILED!")
            continue

        out_path = OUTPUT_DIR / f"run_{i}.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        # 快速摘要
        sm = result.get("section_mapping", {})
        ee = result.get("extracted_entities", {})
        sec_counts = {k: len(v) for k, v in sm.items()}
        ent_counts = {k: len(v) for k, v in ee.items()}
        total_entities = sum(ent_counts.values())
        print(f"  Sections: {sec_counts}")
        print(f"  Entities ({total_entities} total): {ent_counts}")

        if i < N_RUNS:
            time.sleep(2)

    # 对比
    print(f"\n{'='*50}")
    print("COMPARISON")
    runs = []
    for i in range(1, N_RUNS + 1):
        p = OUTPUT_DIR / f"run_{i}.json"
        if p.exists():
            runs.append((i, json.loads(p.read_text(encoding="utf-8"))))

    if len(runs) < 2:
        print("Not enough runs to compare")
        return

    # 1. 对比 section_mapping 的 block 数量分布
    print("\n--- Section block counts ---")
    all_sections = set()
    for _, r in runs:
        all_sections.update(r.get("section_mapping", {}).keys())
    for sec in sorted(all_sections):
        counts = []
        for run_id, r in runs:
            ids = r.get("section_mapping", {}).get(sec, [])
            counts.append(f"run{run_id}:{len(ids)}")
        print(f"  {sec:35s} {', '.join(counts)}")

    # 2. 对比实体数量
    print("\n--- Entity counts ---")
    all_cats = set()
    for _, r in runs:
        all_cats.update(r.get("extracted_entities", {}).keys())
    for cat in sorted(all_cats):
        counts = []
        for run_id, r in runs:
            ents = r.get("extracted_entities", {}).get(cat, [])
            names = [e.get("name_in_paper", "?") for e in ents]
            counts.append(f"run{run_id}({len(ents)}): {names}")
        print(f"  {cat}:")
        for c in counts:
            print(f"    {c}")

    # 3. 对比 block 覆盖率
    print("\n--- Block coverage ---")
    max_id = len(blocks) - 1
    for run_id, r in runs:
        sm = r.get("section_mapping", {})
        covered = set()
        for ids in sm.values():
            covered.update(ids)
        missing = set(range(max_id + 1)) - covered
        dup_check = []
        all_ids = []
        for ids in sm.values():
            all_ids.extend(ids)
        from collections import Counter
        dups = {k: v for k, v in Counter(all_ids).items() if v > 1}
        print(f"  run{run_id}: covered={len(covered)}/{max_id+1}, missing={len(missing)}, dups={dups if dups else 'none'}")


if __name__ == "__main__":
    main()
