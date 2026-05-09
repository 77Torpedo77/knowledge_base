"""共享工具函数 — 配置加载、文件发现、结果保存"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "script" / "config.json"
DATA_DIR = PROJECT_ROOT / "zotero_data"


def load_config(config_path: str | Path | None = None) -> dict:
    """加载 config.json。"""
    path = Path(config_path) if config_path else CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_paper_dirs(data_dir: Path | None = None, limit: int | None = None) -> list[Path]:
    """扫描所有包含 full.md 的论文目录（clear_table 和 pipeline 会自动补齐后续文件）。"""
    data_dir = data_dir or DATA_DIR
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


def save_result(result: dict, llm_raw: dict | None, paper_dir: Path):
    """保存处理结果和 LLM 原始输出到论文目录。"""
    paper_id = result.get("paper_id", paper_dir.name)

    if llm_raw is not None:
        raw_file = paper_dir / f"{paper_id}_llm_raw.json"
        raw_file.write_text(
            json.dumps(llm_raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    out_file = paper_dir / f"{paper_id}.json"
    out_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
