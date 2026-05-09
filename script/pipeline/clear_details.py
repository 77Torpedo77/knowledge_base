"""Phase 0: details 清理 — 删除 MinerU 生成的 <details> 折叠块"""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def remove_details(md_text: str) -> tuple[str, int]:
    """删除 md_text 中所有 <details>...</details> 块。"""
    pattern = re.compile(r"\n*<details>.*?</details>\n*", re.DOTALL)
    matches = list(pattern.finditer(md_text))
    if not matches:
        return md_text, 0

    result = pattern.sub("\n", md_text)
    return result, len(matches)


def clear_details_for_paper(paper_dir: Path, source_name: str = "full.md", target_name: str = "full_clear.md") -> bool:
    """处理单个论文目录：删除 details 块并写入目标文件。"""
    source_path = paper_dir / source_name
    target_path = paper_dir / target_name
    cite_key = paper_dir.name

    if not source_path.exists():
        log.warning("[%s] %s not found, skipping clear_details", cite_key, source_name)
        return False

    md_text = source_path.read_text(encoding="utf-8")
    cleared_text, count = remove_details(md_text)
    target_path.write_text(cleared_text, encoding="utf-8")

    if count:
        log.info("[%s] %d details blocks removed -> %s", cite_key, count, target_name)
    else:
        log.info("[%s] No details blocks, %s copied", cite_key, target_name)
    return True
