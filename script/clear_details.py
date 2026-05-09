"""
清理 Markdown 中的 <details>...</details> 折叠块，生成 full_clear.md（不修改原始 full.md）。

用法:
  python clear_details.py                          # 处理所有包含 details 的 full.md
  python clear_details.py --single yu2025MCVO...   # 只处理指定目录
  python clear_details.py --dry-run                # 预览不写入
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.clear_details import remove_details

SCRIPT_DIR = Path(__file__).parent
DEFAULT_DATA_DIR = SCRIPT_DIR.parent / "zotero_data"


def process_paper(paper_dir: Path, dry_run: bool = False) -> bool:
    """处理单个论文目录。返回是否处理了 details。"""
    full_md = paper_dir / "full.md"
    if not full_md.exists():
        return False

    md_text = full_md.read_text(encoding="utf-8")
    if "<details>" not in md_text:
        return False

    cite_key = paper_dir.name
    clear_path = paper_dir / "full_clear.md"
    cleared_text, count = remove_details(md_text)

    if dry_run:
        print(f"  [{cite_key}] {count} details blocks found (dry-run)")
        return count > 0

    clear_path.write_text(cleared_text, encoding="utf-8")
    print(f"  [{cite_key}] {count} details blocks removed -> full_clear.md")
    return True


def main():
    parser = argparse.ArgumentParser(description="清理 Markdown 中的 details 折叠块")
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

    print(f"\nDone. {processed}/{total} papers had details removed.")


if __name__ == "__main__":
    main()
