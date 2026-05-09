"""Phase 0: 表格清理 — 从 Markdown 提取 HTML 表格"""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def extract_tables(md_text: str, paper_dir: Path) -> tuple[str, int]:
    """从 md_text 中提取所有 <table>...</table>，返回清理后的文本和提取数量。"""
    table_dir = paper_dir / "table"
    pattern = re.compile(r"\n*<table>.*?</table>\n*", re.DOTALL)

    matches = list(pattern.finditer(md_text))
    if not matches:
        return md_text, 0

    table_dir.mkdir(parents=True, exist_ok=True)

    # 从后往前替换，避免偏移
    result = md_text
    for i, match in enumerate(sorted(matches, key=lambda m: m.start(), reverse=True)):
        table_num = len(matches) - i
        table_html = match.group()
        table_content = table_html.strip("\n")

        table_filename = f"table_{table_num}.md"
        table_path = table_dir / table_filename
        table_path.write_text(table_content + "\n", encoding="utf-8")

        result = result[:match.start()] + f"\n![](table/{table_filename})\n" + result[match.end():]

    return result, len(matches)


def clear_tables_for_paper(paper_dir: Path, source_name: str = "full.md", target_name: str = "full_clear_table.md") -> bool:
    """处理单个论文目录：提取 HTML 表格并写入目标文件。"""
    source_path = paper_dir / source_name
    target_path = paper_dir / target_name
    cite_key = paper_dir.name

    if not source_path.exists():
        log.warning("[%s] %s not found, skipping clear_table", cite_key, source_name)
        return False

    md_text = source_path.read_text(encoding="utf-8")
    if "<table>" not in md_text:
        target_path.write_text(md_text, encoding="utf-8")
        log.info("[%s] No HTML tables, %s copied", cite_key, target_name)
        return True

    cleared_text, count = extract_tables(md_text, paper_dir)
    target_path.write_text(cleared_text, encoding="utf-8")
    log.info("[%s] %d tables extracted -> %s", cite_key, count, target_name)
    return True
