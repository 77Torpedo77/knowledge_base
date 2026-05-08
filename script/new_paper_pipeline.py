"""
模块化论文图谱构建管线
Pipeline: Markdown → 物理切块 → LLM语义提取 → 零幻觉校验 → 拼装

用法:
  python new_paper_pipeline.py                          # 处理所有论文
  python new_paper_pipeline.py --limit 5                # 限制数量
  python new_paper_pipeline.py --single pan2025xxx      # 处理指定论文
"""

import argparse
import json
import logging
import time
from pathlib import Path

from openai import OpenAI

from pipeline.chunker import chunk_markdown
from pipeline.extractor import llm_extract
from pipeline.verifier import verify_entities
from pipeline.assembler import assemble, check_coverage
from pipeline.utils import (
    load_config, find_paper_dirs, load_metadata, save_result,
    CONFIG_PATH, DATA_DIR, OUTPUT_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class PaperProcessor:
    """论文处理管线主类。"""

    def __init__(self, config_path: str | Path | None = None):
        config = load_config(config_path)
        api_key = config.get("llm_key")
        if not api_key:
            raise ValueError("'llm_key' not found in config.json")
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    def process_single_paper(self, md_path: Path, zotero_meta: dict) -> tuple[dict, dict | None]:
        """
        处理单篇论文的完整管线。

        Returns:
            (最终结果dict, LLM原始输出dict)
        """
        if not md_path.exists():
            raise FileNotFoundError(f"Markdown file not found: {md_path}")

        text = md_path.read_text(encoding="utf-8")
        cite_key = zotero_meta.get("cite_key", md_path.parent.name)
        log.info("[%s] Starting pipeline — MD size: %d chars", cite_key, len(text))

        # Phase 1: 物理切块
        indexed_blocks = chunk_markdown(text)
        log.info("[%s] Phase 1: %d blocks created", cite_key, len(indexed_blocks))

        if not indexed_blocks:
            log.warning("[%s] No blocks after chunking, skipping", cite_key)
            result = {"paper_id": cite_key, "metadata": {}, "sections": [],
                      "extracted_entities": {}, "error": "empty_content"}
            return result, None

        # 保存送入 LLM 前的对照文件（每行对应一个 block ID）
        before_llm_path = md_path.parent / "full_before_llm.md"
        before_llm_path.write_text(
            "\n".join(b["text"] for b in indexed_blocks),
            encoding="utf-8",
        )
        log.info("[%s] Saved full_before_llm.md (%d lines)", cite_key, len(indexed_blocks))

        # Phase 2: LLM 语义提取（带覆盖率重试）
        max_retries = 3
        llm_result = None
        for attempt in range(1, max_retries + 1):
            log.info("[%s] Phase 2: Calling LLM (attempt %d/%d)...", cite_key, attempt, max_retries)
            llm_result = llm_extract(self.client, indexed_blocks)
            if not llm_result:
                log.error("[%s] LLM returned empty, retrying...", cite_key)
                continue

            missing, duplicates = check_coverage(llm_result, indexed_blocks)
            if not missing and not duplicates:
                break

            log.warning("[%s] Coverage check FAILED on attempt %d — missing: %s, duplicates: %s",
                        cite_key, attempt, missing, duplicates)
            if attempt < max_retries:
                log.info("[%s] Retrying LLM extraction...", cite_key)

        if not llm_result:
            log.error("[%s] LLM extraction failed after %d attempts", cite_key, max_retries)
            return {"paper_id": cite_key, "metadata": {}, "sections": [],
                    "extracted_entities": {}, "error": "llm_failed"}, None

        missing, duplicates = check_coverage(llm_result, indexed_blocks)
        if missing or duplicates:
            log.error("[%s] Coverage still incomplete after %d retries — missing: %s, duplicates: %s",
                      cite_key, max_retries, missing, duplicates)

        # Phase 3: 零幻觉校验
        log.info("[%s] Phase 3: Verifying evidence quotes...", cite_key)
        raw_entities = llm_result.get("extracted_entities", {})
        verified = verify_entities(raw_entities, indexed_blocks)

        # Phase 4: 拼装
        log.info("[%s] Phase 4: Assembling...", cite_key)
        result = assemble(llm_result, verified, indexed_blocks, zotero_meta)

        return result, llm_result


def main():
    parser = argparse.ArgumentParser(description="New Paper Pipeline: Chunk → LLM → Verify → Assemble")
    parser.add_argument("--limit", type=int, default=None, help="限制处理论文数量")
    parser.add_argument("--delay", type=float, default=1.0, help="API 调用间隔（秒）")
    parser.add_argument("--data-dir", type=str, default=str(DATA_DIR), help="论文数据目录")
    parser.add_argument("--output-dir", type=str, default=str(OUTPUT_DIR), help="输出目录")
    parser.add_argument("--single", type=str, default=None, help="只处理指定 cite_key 的论文")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    processor = PaperProcessor()

    if args.single:
        target = data_dir / args.single
        paper_dirs = [target] if target.exists() else []
    else:
        paper_dirs = find_paper_dirs(data_dir, args.limit)

    if not paper_dirs:
        log.error("No papers found in %s", data_dir)
        return

    log.info("Found %d papers to process", len(paper_dirs))

    success = 0
    for i, paper_dir in enumerate(paper_dirs, 1):
        cite_key = paper_dir.name
        log.info("=" * 60)
        log.info("[%d/%d] Processing: %s", i, len(paper_dirs), cite_key)

        meta = load_metadata(paper_dir) or {"cite_key": cite_key}
        md_path = paper_dir / "full_clear_table.md"

        try:
            result, llm_raw = processor.process_single_paper(md_path, meta)
            save_result(result, llm_raw, output_dir)
            if "error" not in result:
                success += 1
        except Exception as e:
            log.error("[%s] Pipeline failed: %s", cite_key, e)

        if i < len(paper_dirs):
            time.sleep(args.delay)

    log.info("=" * 60)
    log.info("Done. %d/%d papers processed successfully. Results in %s",
             success, len(paper_dirs), output_dir)


if __name__ == "__main__":
    main()
