"""
清理 full.md 中的 HTML 表格，将每个表格提取到 table/ 目录下的独立 .md 文件中，
生成 full_clear_table.md（不修改原始 full.md）。

用法:
  python clear_table.py                          # 处理所有包含表格的 full.md
  python clear_table.py --single campos2021...   # 只处理指定目录
  python clear_table.py --dry-run                # 预览不写入
"""

import argparse
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DEFAULT_DATA_DIR = SCRIPT_DIR.parent / "zotero_data"


def extract_tables(md_text: str, paper_dir: Path, dry_run: bool = False) -> tuple[str, int]:
    """从 md_text 中提取所有 <table>...</table>，返回清理后的文本和提取数量。"""
    table_dir = paper_dir / "table"
    pattern = re.compile(r"\n*<table>.*?</table>\n*", re.DOTALL)

    matches = list(pattern.finditer(md_text))
    if not matches:
        return md_text, 0

    if not dry_run:
        table_dir.mkdir(parents=True, exist_ok=True)

    # 从后往前替换，避免偏移
    result = md_text
    for i, match in enumerate(sorted(matches, key=lambda m: m.start(), reverse=True)):
        table_num = len(matches) - i
        table_html = match.group()
        # 去掉首尾换行
        table_content = table_html.strip("\n")

        table_filename = f"table_{table_num}.md"
        table_path = table_dir / table_filename

        if not dry_run:
            table_path.write_text(table_content + "\n", encoding="utf-8")

        # 替换为引用，保留一个换行
        result = result[:match.start()] + f"\n![](table/{table_filename})\n" + result[match.end():]

    return result, len(matches)


def process_paper(paper_dir: Path, dry_run: bool = False) -> bool:
    """处理单个论文目录。返回是否处理了表格。"""
    full_md = paper_dir / "full.md"
    if not full_md.exists():
        return False

    md_text = full_md.read_text(encoding="utf-8")
    if "<table>" not in md_text:
        return False

    cite_key = paper_dir.name
    clear_path = paper_dir / "full_clear_table.md"

    cleared_text, count = extract_tables(md_text, paper_dir, dry_run)

    if count == 0:
        return False

    if dry_run:
        print(f"  [{cite_key}] {count} tables found (dry-run)")
    else:
        clear_path.write_text(cleared_text, encoding="utf-8")
        print(f"  [{cite_key}] {count} tables extracted -> full_clear_table.md")

    return True


def main():
    parser = argparse.ArgumentParser(description="清理 full.md 中的 HTML 表格")
    parser.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--single", type=str, default=None, help="只处理指定 cite_key 目录")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)

    if args.single:
        paper_dirs = [data_dir / args.single]
    else:
        paper_dirs = sorted(d for d in data_dir.iterdir() if d.is_dir())

    total = 0
    processed = 0
    for paper_dir in paper_dirs:
        total += 1
        if process_paper(paper_dir, dry_run=args.dry_run):
            processed += 1

    print(f"\nDone. {processed}/{total} papers had tables extracted.")


if __name__ == "__main__":
    main()
