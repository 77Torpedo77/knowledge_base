"""
基于物理切块与逻辑映射的论文图谱构建管线
Pipeline: Markdown → 物理切块 → LLM语义打标 → 零幻觉拼装与校验
"""

import json
import argparse
import logging
import re
import time
from pathlib import Path

from openai import OpenAI
from thefuzz import fuzz

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "script" / "config.json"
DATA_DIR = PROJECT_ROOT / "zotero_data"
OUTPUT_DIR = PROJECT_ROOT / "pipeline_output"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Phase 1: 物理切块 ───────────────────────────────────────────────


def chunk_markdown(text: str) -> list[dict]:
    """将 Markdown 按行切分为带 ID 的文本块数组，过滤纯空行。"""
    lines = text.split("\n")
    blocks = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            blocks.append({"id": len(blocks), "text": stripped})
    return blocks


# ─── Phase 2: LLM 语义打标 ────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a rigorous academic paper structure analyzer. You will receive a JSON array of text blocks, each with a unique "id" and "text" field. These blocks are raw lines from a paper's Markdown — headers may be missing, formatting may be garbled.

YOUR TASK (output STRICTLY as JSON):
1. **section_mapping**: Group block IDs into exactly these sections:
   - "BACKGROUND": research motivation, problem definition, domain challenges
   - "RELATED_WORK": prior research, literature review, historical context
   - "METHOD": proposed algorithms, models, technical frameworks, system architecture
   - "EXPERIMENT": datasets, experimental setup, results, ablation studies, comparisons
   - "CONCLUSION": summary, contributions, future directions
   - "TRASH": garbled tables, OCR artifacts, page headers/footers, image references like "![](images/...)", isolated numbers/symbols, references/bibliography entries, table of contents

   IMPORTANT RULES FOR section_mapping:
   - Every block ID from 0 to the last ID MUST appear in EXACTLY ONE section.
   - Do NOT skip any ID. Do NOT duplicate any ID.
   - When in doubt, assign to TRASH rather than guessing.

2. **extracted_entities**: Extract named entities mentioned in the paper.
   Each entity must have:
   - "entity_type": one of "PROBLEM", "METHOD", "DATASET", "BASELINE_METHOD"
   - "entity_name": the canonical name (e.g., "CIFAR-10", "ResNet", "ORB-SLAM")
   - "evidence_block_id": the block ID where this entity is mentioned
   - "evidence_quote": a DIRECT COPY-PASTE from the original block text — do NOT paraphrase, reword, or modify even one character

   Entity type definitions:
   - PROBLEM: The research problem/task being addressed (e.g., "object detection", "visual odometry")
   - METHOD: Technical methods/algorithms/models proposed or used (not baseline comparisons)
   - DATASET: Named datasets used in experiments
   - BASELINE_METHOD: Methods used as comparison baselines in experiments

CRITICAL RULES:
- You MUST NOT modify, paraphrase, or reword ANY original text.
- evidence_quote must be an EXACT substring from the block identified by evidence_block_id.
- For section_mapping, use ONLY the numeric IDs — never modify the block text.
- Output valid JSON only. No markdown fences, no explanatory text.
"""


def llm_extract(client: OpenAI, indexed_blocks: list[dict],
                 model: str = "deepseek-v4-flash") -> dict | None:
    """调用 DeepSeek API 进行语义打标和实体提取。"""
    blocks_text = json.dumps(indexed_blocks, ensure_ascii=False)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Analyze the following paper blocks and output the JSON:\n\n{blocks_text}"},
    ]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=8192,
        )
        content = response.choices[0].message.content
        if not content or not content.strip():
            log.warning("LLM returned empty response")
            return None
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.error("JSON parse failed: %s", e)
        return None
    except Exception as e:
        log.error("API call failed: %s", e)
        return None


# ─── Phase 3: 零幻觉拼装与模糊校验 ──────────────────────────────────


def _split_sentences(text: str) -> list[str]:
    """将文本按句号、问号、感叹号切分为句子列表。"""
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in parts if s.strip()]


def _verify_evidence(entity: dict, indexed_blocks: list[dict]) -> tuple[bool, str | None]:
    """
    校验 evidence_quote 是否存在于对应的原始文本块中。
    返回 (是否通过, 替换后的quote 或 None 表示应丢弃)。
    """
    block_id = entity.get("evidence_block_id")
    quote = entity.get("evidence_quote", "")

    if block_id is None or block_id < 0 or block_id >= len(indexed_blocks):
        log.warning("Entity '%s' has invalid evidence_block_id=%s, dropped",
                    entity.get("entity_name"), block_id)
        return False, None

    original_text = indexed_blocks[block_id]["text"]

    # Exact match
    if quote in original_text:
        return True, quote

    # Fuzzy match: 按句子级别比对
    sentences = _split_sentences(original_text)
    if not sentences:
        sentences = [original_text]

    best_ratio = 0
    best_match = None
    for sent in sentences:
        ratio = fuzz.ratio(quote, sent) / 100.0
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = sent

    if best_ratio >= 0.85:
        log.info("Fuzzy match for '%s': %.1f%% → replaced quote",
                 entity.get("entity_name"), best_ratio * 100)
        return True, best_match

    log.warning("HALLUCINATION: '%s' quote='%.60s...' best=%.1f%%, DROPPED",
                entity.get("entity_name"), quote, best_ratio * 100)
    return False, None


def assemble_and_verify(llm_result: dict, indexed_blocks: list[dict]) -> dict:
    """
    Phase 3: 拼装 Section 内容，校验 evidence_quote，丢弃幻觉实体。
    """
    section_mapping = llm_result.get("section_mapping", {})
    raw_entities = llm_result.get("extracted_entities", [])

    # 1. 拼装 Section 内容（忽略 TRASH）
    sections = {}
    block_map = {b["id"]: b["text"] for b in indexed_blocks}

    valid_sections = ["BACKGROUND", "RELATED_WORK", "METHOD", "EXPERIMENT", "CONCLUSION"]
    for sec_name in valid_sections:
        ids = section_mapping.get(sec_name, [])
        parts = [block_map[bid] for bid in ids if bid in block_map]
        sections[sec_name] = "\n".join(parts)

    # 2. 校验 Evidence Quote
    verified_entities = []
    for entity in raw_entities:
        passed, replacement_quote = _verify_evidence(entity, indexed_blocks)
        if passed:
            entity["evidence_quote"] = replacement_quote
            verified_entities.append(entity)

    return {
        "sections": sections,
        "entities": verified_entities,
        "trash_ids": section_mapping.get("TRASH", []),
    }


# ─── PaperProcessor 主类 ────────────────────────────────────────────


class PaperProcessor:
    """论文处理管线主类，暴露 process_single_paper 接口。"""

    def __init__(self, config_path: str | Path | None = None):
        config_path = Path(config_path) if config_path else CONFIG_PATH
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        api_key = self.config.get("llm_key")
        if not api_key:
            raise ValueError("'llm_key' not found in config.json")

        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    def process_single_paper(self, pdf_md_path: str | Path, zotero_meta: dict) -> dict:
        """
        处理单篇论文的完整管线。

        Args:
            pdf_md_path: full.md 文件路径
            zotero_meta: Zotero 导出的论文元数据 dict

        Returns:
            处理结果 dict，包含 sections, entities, meta 信息
        """
        md_path = Path(pdf_md_path)
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
            return {"cite_key": cite_key, "sections": {}, "entities": [], "error": "empty_content"}

        # Phase 2: LLM 语义打标
        log.info("[%s] Phase 2: Calling LLM...", cite_key)
        llm_result = llm_extract(self.client, indexed_blocks)
        if not llm_result:
            log.error("[%s] LLM extraction failed", cite_key)
            return {"cite_key": cite_key, "sections": {}, "entities": [], "error": "llm_failed"}

        # Phase 3: 拼装与校验
        log.info("[%s] Phase 3: Assembling and verifying...", cite_key)
        assembled = assemble_and_verify(llm_result, indexed_blocks)

        total_entities = len(llm_result.get("extracted_entities", []))
        verified_entities = len(assembled["entities"])
        log.info(
            "[%s] Done — Sections: %s | Entities: %d/%d verified | TRASH blocks: %d",
            cite_key,
            {k: len(v.split(chr(10))) for k, v in assembled["sections"].items() if v},
            verified_entities, total_entities,
            len(assembled.get("trash_ids", [])),
        )

        return {
            "cite_key": cite_key,
            "sections": assembled["sections"],
            "entities": assembled["entities"],
            "trash_ids": assembled["trash_ids"],
            "block_count": len(indexed_blocks),
            "meta": {
                "title": zotero_meta.get("title", ""),
                "year": (zotero_meta.get("date") or "")[:4],
                "journal": zotero_meta.get("publicationTitle", ""),
                "doi": zotero_meta.get("DOI", ""),
                "authors": [
                    f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                    for c in zotero_meta.get("creators", [])
                ],
            },
        }


# ─── 辅助函数 ────────────────────────────────────────────────────────


def find_paper_dirs(data_dir: Path, limit: int | None = None) -> list[Path]:
    """扫描所有包含 full.md 的论文目录。"""
    dirs = sorted(
        d for d in data_dir.iterdir()
        if d.is_dir() and (d / "full.md").exists()
    )
    if limit is not None:
        dirs = dirs[:limit]
    return dirs


def load_metadata(paper_dir: Path) -> dict | None:
    """从论文目录读取 metadata.json。"""
    meta_path = paper_dir / "metadata.json"
    if not meta_path.exists():
        return None
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_result(result: dict, output_dir: Path):
    """保存处理结果为 JSON 文件。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    cite_key = result.get("cite_key", "unknown")
    out_file = output_dir / f"{cite_key}.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── CLI 入口 ────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Paper Pipeline: Chunk → LLM → Verify")
    parser.add_argument("--limit", type=int, default=None, help="限制处理论文数量")
    parser.add_argument("--delay", type=float, default=1.0, help="API 调用间隔（秒）")
    parser.add_argument("--data-dir", type=str, default=str(DATA_DIR), help="论文数据目录")
    parser.add_argument("--output-dir", type=str, default=str(OUTPUT_DIR), help="输出目录")
    parser.add_argument("--single", type=str, default=None, help="只处理指定 cite_key 的论文")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    processor = PaperProcessor()

    # 确定要处理的论文
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
        md_path = paper_dir / "full.md"

        try:
            result = processor.process_single_paper(md_path, meta)
            save_result(result, output_dir)
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
