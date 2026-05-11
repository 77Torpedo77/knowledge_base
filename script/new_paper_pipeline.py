"""
模块化论文图谱构建管线
Pipeline: Markdown → 表格/details清理 → 物理切块 → LLM语义提取 → 零幻觉校验 → 拼装

用法:
  python new_paper_pipeline.py                          # 处理所有论文（串行）
  python new_paper_pipeline.py --workers 5              # 5 篇并行
  python new_paper_pipeline.py --limit 10 --workers 5   # 取 10 篇，5 并行
  python new_paper_pipeline.py --single pan2025xxx      # 处理指定论文

前置要求: zotero_data/{cite_key}/full.md 需已存在（由 mineru_zotero_parser.py 生成）。
管线会自动执行预处理（full.md → full_clear.md）。
"""

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from openai import OpenAI

from pipeline.chunker import chunk_markdown
from pipeline.extractor import llm_extract
from pipeline.verifier import verify_entities
from pipeline.assembler import assemble, check_coverage, validate_llm_output
from pipeline.clear_table import clear_tables_for_paper
from pipeline.clear_details import clear_details_for_paper
from pipeline.utils import (
    load_config, find_paper_dirs, load_metadata, save_result,
    CONFIG_PATH, DATA_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class PaperProcessor:
    """论文处理管线主类。"""

    def __init__(self, config_path: str | Path | None = None, *, verbose: bool = True):
        config = load_config(config_path)
        api_key = config.get("llm_key")
        if not api_key:
            raise ValueError("'llm_key' not found in config.json")
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self.verbose = verbose

    def process_single_paper(self, paper_dir: Path, zotero_meta: dict,
                             *, on_llm_progress=None, on_status=None) -> tuple[dict, dict | None]:
        """处理单篇论文的完整管线。

        Args:
            paper_dir: 论文目录
            zotero_meta: Zotero 元数据
            on_llm_progress: callable(phase, chars) — LLM 流式进度回调
            on_status: callable(status_text) — 管线阶段状态回调
        """
        cite_key = zotero_meta.get("cite_key", paper_dir.name)

        # Phase 0: 预处理
        clear_tables_for_paper(paper_dir, source_name="full.md", target_name="full_clear_table.md")
        clear_details_for_paper(paper_dir, source_name="full_clear_table.md", target_name="full_clear.md")
        if on_status:
            on_status("preprocessed")

        md_path = paper_dir / "full_clear.md"
        if not md_path.exists():
            log.error("[%s] full_clear.md not found after preprocessing", cite_key)
            if on_status:
                on_status("[red]✗ no full_clear.md")
            return {"paper_id": cite_key, "metadata": {}, "sections": [],
                    "extracted_entities": {}, "error": "no_full_clear"}, None

        text = md_path.read_text(encoding="utf-8")
        log.info("[%s] Starting pipeline — MD size: %d chars", cite_key, len(text))

        # Phase 1: 物理切块
        indexed_blocks = chunk_markdown(text)
        log.info("[%s] Phase 1: %d blocks created", cite_key, len(indexed_blocks))
        if on_status:
            on_status(f"Phase 1: {len(indexed_blocks)} blocks")

        if not indexed_blocks:
            log.warning("[%s] No blocks after chunking, skipping", cite_key)
            result = {"paper_id": cite_key, "metadata": {}, "sections": [],
                      "extracted_entities": {}, "error": "empty_content"}
            return result, None

        before_llm_path = paper_dir / "full_before_llm.md"
        before_llm_path.write_text(
            "\n".join(b["text"] for b in indexed_blocks),
            encoding="utf-8",
        )
        log.info("[%s] Saved full_before_llm.md (%d lines)", cite_key, len(indexed_blocks))

        # Phase 2: LLM 语义提取
        max_retries = 3
        llm_result = None
        for attempt in range(1, max_retries + 1):
            log.info("[%s] Phase 2: Calling LLM (attempt %d/%d)...", cite_key, attempt, max_retries)
            if on_status:
                on_status(f"Phase 2: LLM attempt {attempt}/{max_retries}")
            llm_result = llm_extract(self.client, indexed_blocks, verbose=self.verbose,
                                      on_progress=on_llm_progress)
            if not llm_result:
                log.error("[%s] LLM returned empty, retrying...", cite_key)
                continue

            validate_llm_output(llm_result)

            missing, duplicates = check_coverage(llm_result, indexed_blocks)
            if not missing and not duplicates:
                break

            log.warning("[%s] Coverage check FAILED on attempt %d — missing: %s, duplicates: %s",
                        cite_key, attempt, missing, duplicates)
            if attempt < max_retries:
                log.info("[%s] Retrying LLM extraction...", cite_key)

        if not llm_result:
            log.error("[%s] LLM extraction failed after %d attempts", cite_key, max_retries)
            if on_status:
                on_status("[red]✗ LLM failed")
            return {"paper_id": cite_key, "metadata": {}, "sections": [],
                    "extracted_entities": {}, "error": "llm_failed"}, None

        missing, duplicates = check_coverage(llm_result, indexed_blocks)
        if missing or duplicates:
            log.error("[%s] Coverage still incomplete after %d retries — missing: %s, duplicates: %s",
                      cite_key, max_retries, missing, duplicates)

        # Phase 3: 零幻觉校验
        log.info("[%s] Phase 3: Verifying evidence quotes...", cite_key)
        if on_status:
            on_status("Phase 3: verifying")
        raw_entities = llm_result.get("extracted_entities", {})
        verified = verify_entities(raw_entities, indexed_blocks)

        # Phase 4: 拼装
        log.info("[%s] Phase 4: Assembling...", cite_key)
        if on_status:
            on_status("Phase 4: assembling")
        result = assemble(llm_result, verified, indexed_blocks, zotero_meta)

        return result, llm_result


def _count_entities(result: dict) -> int:
    return sum(len(v) for v in result.get("extracted_entities", {}).values() if isinstance(v, list))


def _run_serial(processor: PaperProcessor, paper_dirs: list[Path], delay: float):
    """串行模式：逐篇处理，保留 stdout 流式 LLM 进度。"""
    success = 0
    for i, paper_dir in enumerate(paper_dirs, 1):
        cite_key = paper_dir.name
        log.info("=" * 60)
        log.info("[%d/%d] Processing: %s", i, len(paper_dirs), cite_key)
        meta = load_metadata(paper_dir) or {"cite_key": cite_key}
        try:
            result, llm_raw = processor.process_single_paper(paper_dir, meta)
            save_result(result, llm_raw, paper_dir)
            if "error" not in result:
                success += 1
        except Exception as e:
            log.error("[%s] Pipeline failed: %s", cite_key, e)
        if i < len(paper_dirs):
            time.sleep(delay)
    return success


def _run_parallel(processor: PaperProcessor, paper_dirs: list[Path], workers: int, delay: float):
    """并行模式：仅显示当前活跃任务，完成后移除（uv 风格）。"""
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    root = logging.getLogger()
    orig_level = root.level
    root.setLevel(logging.WARNING)

    total = len(paper_dirs)
    success = 0
    completed = 0

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.fields[name]}"),
        TextColumn("  {task.fields[status]}"),
        TimeElapsedColumn(),
        refresh_per_second=4,
    )

    with progress:
        summary_id = progress.add_task(
            "", total=total,
            name="[bold green]Progress",
            status=f"0/{total} done",
        )

        remaining = list(paper_dirs)
        active = {}  # future -> (task_id, cite_key)

        def _submit(paper_dir: Path):
            cite_key = paper_dir.name
            tid = progress.add_task("", total=1, name=cite_key, status="waiting...")
            meta = load_metadata(paper_dir) or {"cite_key": cite_key}

            def on_llm_progress(phase, chars):
                label = "reasoning" if phase == "thinking" else "generating"
                progress.update(tid, status=f"LLM [cyan]{label}[/cyan] {chars:,} chars")

            def on_status(status):
                progress.update(tid, status=status)

            def job():
                try:
                    result, llm_raw = processor.process_single_paper(
                        paper_dir, meta,
                        on_llm_progress=on_llm_progress,
                        on_status=on_status,
                    )
                    save_result(result, llm_raw, paper_dir)
                    ok = "error" not in result
                    n = _count_entities(result)
                    return ok, n
                except Exception as e:
                    log.error("[%s] Pipeline failed: %s", cite_key, e)
                    return False, 0

            future = executor.submit(job)
            active[future] = (tid, cite_key)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            for _ in range(min(workers, len(remaining))):
                _submit(remaining.pop(0))
                if delay > 0:
                    time.sleep(delay)

            while active:
                done, _ = wait(active.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    tid, cite_key = active.pop(future)
                    ok, n = future.result()
                    progress.remove_task(tid)
                    if ok:
                        success += 1
                    completed += 1
                    progress.update(
                        summary_id, completed=completed,
                        name="[bold green]Progress",
                        status=f"{completed}/{total} done ({success} [green]✓[/green])",
                    )
                    if remaining:
                        _submit(remaining.pop(0))
                        if delay > 0:
                            time.sleep(delay)

    root.setLevel(orig_level)
    return success


def main():
    parser = argparse.ArgumentParser(description="Paper Pipeline: Preprocess → Chunk → LLM → Verify → Assemble")
    parser.add_argument("--limit", type=int, default=None, help="限制处理论文数量")
    parser.add_argument("--workers", type=int, default=1, help="并行 LLM 调用数（默认 1 即串行）")
    parser.add_argument("--delay", type=float, default=1.0, help="任务提交间隔（秒）")
    parser.add_argument("--data-dir", type=str, default=str(DATA_DIR), help="论文数据目录")
    parser.add_argument("--single", type=str, default=None, help="只处理指定 cite_key 的论文")
    parser.add_argument("--force", action="store_true", help="强制重新执行，忽略已完成的论文")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    verbose = args.workers <= 1
    processor = PaperProcessor(verbose=verbose)

    if args.single:
        target = data_dir / args.single
        if not target.exists() or not (target / "full.md").exists():
            log.error("Paper not found: %s (need full.md)", target)
            return
        paper_dirs = [target]
    else:
        paper_dirs = find_paper_dirs(data_dir, args.limit)

    if not paper_dirs:
        log.error("No papers found in %s", data_dir)
        return

    log.info("Found %d papers to process (workers=%d)", len(paper_dirs), args.workers)

    # 过滤已完成的论文
    if not args.force:
        todo, skipped = [], []
        for d in paper_dirs:
            out = d / f"{d.name}.json"
            (skipped if out.exists() else todo).append(d)
        if skipped:
            log.info("Skipping %d already completed papers (use --force to re-run)", len(skipped))
        paper_dirs = todo
        if not paper_dirs:
            log.info("All papers already completed, nothing to do.")
            return
    else:
        log.info("--force mode: re-processing all papers")

    if args.workers <= 1:
        success = _run_serial(processor, paper_dirs, args.delay)
    else:
        success = _run_parallel(processor, paper_dirs, args.workers, args.delay)

    log.info("=" * 60)
    log.info("Done. %d/%d papers processed successfully. Results saved in paper directories under %s",
             success, len(paper_dirs), data_dir)


if __name__ == "__main__":
    main()
