"""
MinerU Zotero PDF Batch Parser
从 Zotero 库中提取期刊/会议论文 PDF，使用 MinerU API 解析为结构化 Markdown/JSON。

已实现的功能：
  1. Zotero 本地 API（无需 API Key）获取所有期刊/会议论文
  2. BibTeX file 字段解析 PDF 路径，自动处理 UTF-8 编码
  3. 原始 PDF 智能识别：
    - 英文检测（从 PDF 文本流判断，非二进制头部）
    - 翻译版文件名降级（_translated、_onlyTrans、翻译 等）
    - 最早 mtime 优先
  4. MinerU 精准解析 API（vlm 模式）批量上传、轮询、下载解压
  5. 断点续传：跳过已存在的输出目录 + progress.json 进度记录
  6. 命令行参数：--dry-run、--limit N、--retry-failed
  7. --update-metadata：为已解析的条目补全 Zotero 元数据（不重新解析）
  8. --force：与 --update-metadata 搭配，覆盖已存在的 metadata.json

使用方式：
  python mineru_zotero_parser.py --dry-run                     # 预览
  python mineru_zotero_parser.py --limit 5                     # 先解析 5 个
  python mineru_zotero_parser.py                               # 全量运行
  python mineru_zotero_parser.py --retry-failed                # 重试失败项
  python mineru_zotero_parser.py --update-metadata             # 补全缺失元数据
  python mineru_zotero_parser.py --update-metadata --force     # 强制重写所有 metadata.json
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests

SCRIPT_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"

ZOTERO_API_BASE = "http://localhost:23119/api/users/0"
MINERU_API_BASE = "https://mineru.net/api/v4"

DEFAULT_CONFIG = {
    "zotero_api_base": ZOTERO_API_BASE,
    "zotero_data_dir": r"D:\Zotero_data_from_C",
    "mineru_token_file": r"D:\tools\minerU-token.txt",
    "output_dir": r"D:\tools\knowledge_base\zotero_data",
    "model_version": "vlm",
    "language": "en",
    "batch_size": 10,
    "poll_interval": 10,
    "poll_timeout": 600,
}

logger = logging.getLogger("mineru_parser")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> dict:
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        logger.info("已加载配置: %s", config_path)
        return cfg
    cfg = dict(DEFAULT_CONFIG)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    logger.info("已生成默认配置: %s", config_path)
    return cfg


def read_mineru_token(token_file: str) -> str:
    path = Path(token_file)
    if not path.exists():
        raise FileNotFoundError(f"MinerU Token 文件不存在: {token_file}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("MinerU Token 文件为空")
    return token


# ---------------------------------------------------------------------------
# Zotero Local API
# ---------------------------------------------------------------------------

def zotero_get(endpoint: str, cfg: dict, params: dict | None = None) -> requests.Response:
    url = f"{cfg['zotero_api_base']}{endpoint}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp


def get_all_items_bibtex(cfg: dict, item_type: str) -> list[dict]:
    """获取指定类型的所有条目（BibTeX 格式）"""
    results = []
    start = 0
    limit = 100
    while True:
        resp = zotero_get("/items", cfg, params={
            "itemType": item_type,
            "format": "bibtex",
            "limit": limit,
            "start": start,
        })
        raw = resp.text.strip()
        if not raw:
            break
        entries = re.split(r"\n(?=@)", raw)
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            m = re.match(r"@\w+\{([^,]+),", entry)
            if not m:
                continue
            cite_key = m.group(1)
            results.append({
                "cite_key": cite_key,
                "bibtex_raw": entry,
            })
        if len(entries) < limit:
            break
        start += limit
    return results


def get_item_children(cfg: dict, item_key: str) -> list[dict]:
    """获取条目的子附件列表（fallback 方式）"""
    resp = zotero_get(f"/items/{item_key}/children", cfg)
    items = resp.json()
    results = []
    for item in items:
        d = item.get("data", {})
        results.append({
            "key": d.get("key"),
            "contentType": d.get("contentType", ""),
            "filename": d.get("filename", ""),
            "dateAdded": d.get("dateAdded", ""),
        })
    return results


def get_items_json(cfg: dict, item_type: str) -> list[dict]:
    """获取指定类型的所有条目（JSON 格式），用于拿到 item_key 与 citationKey。"""
    results = []
    start = 0
    limit = 100
    while True:
        resp = zotero_get("/items", cfg, params={
            "itemType": item_type,
            "format": "json",
            "limit": limit,
            "start": start,
        })
        items = resp.json()
        if not items:
            break
        for item in items:
            d = item.get("data", {})
            results.append({
                "key": d.get("key"),
                "title": d.get("title", ""),
                "itemType": d.get("itemType", ""),
                "citationKey": d.get("citationKey", ""),
            })
        if len(items) < limit:
            break
        start += limit
    return results


# ---------------------------------------------------------------------------
# BibTeX file field parsing
# ---------------------------------------------------------------------------

def parse_bibtex_file_field(bibtex_raw: str, zotero_data_dir: str) -> list[dict]:
    """从 BibTeX 的 file 字段解析 PDF 路径列表"""
    m = re.search(r'file\s*=\s*\{(.+?)\}', bibtex_raw, re.DOTALL)
    if not m:
        return []

    file_value = m.group(1)
    pdfs = []

    # 按 ; 分割多个附件
    parts = file_value.split(";")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 格式: title:path:content_type
        # 路径中包含转义的冒号 D\:
        # 用正则匹配 content_type（最后一段）
        ct_match = re.search(r':(application/\w+|text/\w+|image/\w+)$', part)
        if not ct_match:
            continue
        content_type = ct_match.group(1)
        if content_type != "application/pdf":
            continue

        before_ct = part[:ct_match.start()]
        # 按第一个 : 分割 title 和 path
        colon_idx = before_ct.find(":")
        if colon_idx == -1:
            continue
        raw_path = before_ct[colon_idx + 1:]
        # 反转义: \\ -> \, \: -> :
        raw_path = raw_path.replace("\\\\", "/").replace("\\:", ":")
        # 替换路径分隔符
        raw_path = raw_path.replace("\\", "/")

        filename = os.path.basename(raw_path)
        pdfs.append({
            "local_path": raw_path,
            "filename": filename,
        })

    return pdfs


# ---------------------------------------------------------------------------
# PDF Original Version Detection
# ---------------------------------------------------------------------------

def is_english_pdf(file_path: str) -> bool:
    """检查 PDF 内容是否主要是英文。从 PDF 中提取文本流而非解析二进制头部。"""
    try:
        with open(file_path, "rb") as f:
            data = f.read(65536)  # 读前 64KB
        # 提取 PDF 中的文本流（介于 BT...ET 之间的 TJ/Tj 操作符内容）
        text_parts = re.findall(rb"\(([^)]+)\)", data)
        text = b" ".join(text_parts).decode("latin-1", errors="ignore")
        if not text.strip():
            # fallback: 提取所有可打印 ASCII 段
            text = ""
            current = []
            for b in data:
                if 32 <= b < 127:
                    current.append(chr(b))
                else:
                    if len(current) > 20:
                        text += "".join(current) + " "
                    current = []
            if len(current) > 20:
                text += "".join(current)

        if not text.strip():
            return True  # 无法判断时默认英文（避免误判）

        alpha_count = sum(1 for c in text if c.isascii() and c.isalpha())
        # 统计 CJK 字符
        cjk_count = 0
        for c in text:
            cp = ord(c)
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
                    or 0x2E80 <= cp <= 0x2EFF):
                cjk_count += 1
        total_alpha = alpha_count + cjk_count
        if total_alpha == 0:
            return True
        return (alpha_count / total_alpha) > 0.7
    except Exception:
        return True  # 出错时默认英文


_TRANSLATION_MARKERS = re.compile(
    r"[_-](translated|translation|onlyTrans|trans|翻译)", re.IGNORECASE
)


def _is_translation_filename(filename: str) -> bool:
    """通过文件名判断是否为翻译版"""
    return bool(_TRANSLATION_MARKERS.search(filename))


def select_original_pdf(pdfs: list[dict]) -> dict | None:
    """从多个 PDF 中选择原始版本（mtime 最早的英文 PDF，跳过翻译版）"""
    if not pdfs:
        return None

    # 过滤掉不存在的
    existing = [p for p in pdfs if os.path.exists(p["local_path"])]
    if not existing:
        return None
    if len(existing) == 1:
        return existing[0]

    # 计算优先级分数：英文 > 非翻译 > 早期
    for p in existing:
        try:
            p["mtime"] = os.path.getmtime(p["local_path"])
        except OSError:
            p["mtime"] = float("inf")
        p["is_english"] = is_english_pdf(p["local_path"])
        p["is_translation"] = _is_translation_filename(p["filename"])

    # 排序：英文优先 > 非翻译优先 > mtime 最早
    existing.sort(key=lambda x: (
        not x["is_english"],
        x["is_translation"],
        x["mtime"],
    ))

    best = existing[0]
    if best["is_translation"]:
        logger.warning("最佳候选仍为翻译版: %s", best["filename"])
    if not best["is_english"] and not best["is_translation"]:
        logger.debug("非英文原文: %s", best["filename"])
    return best


# ---------------------------------------------------------------------------
# MinerU API
# ---------------------------------------------------------------------------

def mineru_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def mineru_batch_upload(token: str, files: list[dict], cfg: dict) -> str:
    """批量上传 PDF 到 MinerU，返回 batch_id"""
    url = f"{MINERU_API_BASE}/file-urls/batch"
    data = {
        "files": [{"name": f["filename"], "data_id": f["cite_key"]} for f in files],
        "model_version": cfg["model_version"],
        "language": cfg["language"],
    }
    resp = requests.post(url, headers=mineru_headers(token), json=data, timeout=60)
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"MinerU 批量上传请求失败: {result.get('msg', resp.text)}")

    batch_id = result["data"]["batch_id"]
    file_urls = result["data"]["file_urls"]

    for i, f in enumerate(files):
        logger.info("  上传: %s -> %s", f["cite_key"], f["filename"])
        with open(f["local_path"], "rb") as fh:
            put_resp = requests.put(file_urls[i], data=fh, timeout=300)
            if put_resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"文件上传失败 [{put_resp.status_code}]: {f['filename']}"
                )

    return batch_id


def mineru_poll_batch(token: str, batch_id: str, cfg: dict) -> list[dict]:
    """轮询批量任务结果"""
    url = f"{MINERU_API_BASE}/extract-results/batch/{batch_id}"
    interval = cfg.get("poll_interval", 10)
    timeout = cfg.get("poll_timeout", 600)
    start_time = time.time()

    while True:
        resp = requests.get(url, headers=mineru_headers(token), timeout=30)
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"MinerU 轮询失败: {result.get('msg', resp.text)}")

        extract_results = result["data"].get("extract_result", [])
        all_done = all(r["state"] in ("done", "failed") for r in extract_results)
        done_count = sum(1 for r in extract_results if r["state"] == "done")
        total = len(extract_results)

        elapsed = int(time.time() - start_time)
        logger.info("  轮询 [%ds]: %d/%d 完成", elapsed, done_count, total)

        if all_done:
            return [
                {
                    "data_id": r.get("data_id", ""),
                    "file_name": r.get("file_name", ""),
                    "state": r["state"],
                    "zip_url": r.get("full_zip_url", ""),
                    "err_msg": r.get("err_msg", ""),
                }
                for r in extract_results
            ]

        if time.time() - start_time > timeout:
            raise TimeoutError(f"轮询超时 ({timeout}s)，batch_id: {batch_id}")

        time.sleep(interval)


def download_and_extract_zip(zip_url: str, output_dir: str) -> bool:
    """下载 zip 并解压到指定目录"""
    try:
        resp = requests.get(zip_url, timeout=120, stream=True)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(output_dir)
        return True
    except Exception as e:
        logger.error("下载/解压失败: %s", e)
        return False


# ---------------------------------------------------------------------------
# Progress Management
# ---------------------------------------------------------------------------

def load_progress(output_dir: str) -> dict:
    progress_file = os.path.join(output_dir, "progress.json")
    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed": {}, "failed": {}, "skipped": {}}


def save_progress(output_dir: str, progress: dict):
    progress["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    progress_file = os.path.join(output_dir, "progress.json")
    os.makedirs(output_dir, exist_ok=True)
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# cite_key -> item_key matching
# ---------------------------------------------------------------------------

def build_cite_key_to_item_key_map(cfg: dict) -> dict:
    """构建 {citationKey: item_key} 映射（从 JSON API）。"""
    cite_key_map = {}
    for item_type in ("journalArticle", "conferencePaper", "preprint"):
        items = get_items_json(cfg, item_type)
        for item in items:
            citation_key = (item.get("citationKey") or "").strip()
            item_key = item.get("key")
            if citation_key and item_key:
                cite_key_map[citation_key] = item_key
    return cite_key_map


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def collect_entries(cfg: dict, cite_key_map: dict, item_type_filter: str | None = None) -> list[dict]:
    """获取所有期刊和会议论文条目，并按 cite_key 直接匹配 item_key。"""
    all_entries = []
    types = [item_type_filter] if item_type_filter else ("journalArticle", "conferencePaper", "preprint")
    for item_type in types:
        logger.info("获取 %s 条目...", item_type)
        entries = get_all_items_bibtex(cfg, item_type)
        logger.info("  找到 %d 条 %s", len(entries), item_type)

        for entry in entries:
            entry["item_key"] = cite_key_map.get(entry["cite_key"])
            all_entries.append(entry)

    return all_entries


def resolve_pdf(entry: dict, cfg: dict) -> dict | None:
    """为条目找到原始 PDF"""
    cite_key = entry["cite_key"]

    # 首先从 BibTeX file 字段解析
    pdfs = parse_bibtex_file_field(entry["bibtex_raw"], cfg["zotero_data_dir"])

    # 如果 BibTeX 没有解析到 PDF，尝试 children API（需要 item_key）
    if not pdfs and entry.get("item_key"):
        logger.debug("BibTeX file 字段无 PDF，使用 children API: %s", cite_key)
        children = get_item_children(cfg, entry["item_key"])
        for c in children:
            if c["contentType"] == "application/pdf":
                local_path = os.path.join(
                    cfg["zotero_data_dir"], "storage", c["key"], c["filename"]
                )
                pdfs.append({
                    "local_path": local_path,
                    "filename": c["filename"],
                    "mtime": 0,
                })
        # 用 dateAdded 排序
        if len(pdfs) > 1:
            for i, c in enumerate(
                sorted(
                    [c for c in children if c["contentType"] == "application/pdf"],
                    key=lambda x: x["dateAdded"],
                )
            ):
                for p in pdfs:
                    if p["filename"] == c["filename"]:
                        try:
                            p["mtime"] = os.path.getmtime(p["local_path"])
                        except OSError:
                            p["mtime"] = float("inf")

    if not pdfs:
        return None

    pdf_info = select_original_pdf(pdfs)
    if pdf_info is None:
        return None

    if not os.path.exists(pdf_info["local_path"]):
        logger.warning("PDF 文件不存在: %s", pdf_info["local_path"])
        return None

    return {
        "cite_key": cite_key,
        "item_key": entry.get("item_key"),
        "filename": pdf_info["filename"],
        "local_path": pdf_info["local_path"],
    }


def process_batch(token: str, batch: list[dict], cfg: dict, progress: dict, output_dir: str):
    """处理一批 PDF：上传 -> 轮询 -> 下载解压"""
    logger.info("提交 %d 个文件到 MinerU...", len(batch))
    try:
        batch_id = mineru_batch_upload(token, batch, cfg)
    except Exception as e:
        logger.error("批量上传失败: %s", e)
        for f in batch:
            progress["failed"][f["cite_key"]] = str(e)
        return

    logger.info("轮询 batch_id: %s", batch_id)
    try:
        results = mineru_poll_batch(token, batch_id, cfg)
    except Exception as e:
        logger.error("轮询失败: %s", e)
        for f in batch:
            progress["failed"][f["cite_key"]] = str(e)
        return

    for r in results:
        cite_key = r["data_id"] or r.get("file_name", "unknown")
        if r["state"] == "done":
            dest = os.path.join(output_dir, cite_key)
            os.makedirs(dest, exist_ok=True)
            ok = download_and_extract_zip(r["zip_url"], dest)
            if ok:
                progress["processed"][cite_key] = "done"
                logger.info("  完成: %s", cite_key)
            else:
                progress["failed"][cite_key] = "download/extract failed"
        else:
            progress["failed"][cite_key] = r.get("err_msg", "unknown error")
            logger.error("  失败: %s - %s", cite_key, r.get("err_msg", ""))


# ---------------------------------------------------------------------------
# Metadata Update
# ---------------------------------------------------------------------------

def fetch_item_metadata(cfg: dict, item_key: str) -> dict | None:
    """通过 Zotero API 获取条目的完整元数据"""
    try:
        resp = zotero_get(f"/items/{item_key}", cfg)
        return resp.json().get("data")
    except Exception as e:
        logger.error("获取元数据失败 [%s]: %s", item_key, e)
        return None


def update_metadata(cfg: dict, output_dir: str, entries: list[dict], force: bool = False):
    """为已解析的条目目录补全或重写 Zotero 元数据。"""
    cite_to_key = {e["cite_key"]: e.get("item_key") for e in entries if e.get("item_key")}

    # 扫描输出目录
    dirs = [d for d in os.listdir(output_dir)
            if os.path.isdir(os.path.join(output_dir, d)) and d != "__pycache__"]

    updated = 0
    skipped = 0
    no_key = 0

    for i, cite_key in enumerate(dirs):
        dest_dir = os.path.join(output_dir, cite_key)
        meta_file = os.path.join(dest_dir, "metadata.json")

        if os.path.exists(meta_file) and not force:
            skipped += 1
            continue

        if os.path.exists(meta_file) and force:
            logger.info("[%d/%d] 覆盖元数据: %s", i + 1, len(dirs), cite_key)
        else:
            logger.info("[%d/%d] 获取元数据: %s", i + 1, len(dirs), cite_key)


        item_key = cite_to_key.get(cite_key)
        if not item_key:
            logger.warning("  [%d/%d] 无法映射 item_key: %s", i + 1, len(dirs), cite_key)
            no_key += 1
            continue

        data = fetch_item_metadata(cfg, item_key)
        if data is None:
            continue

        # 只保留有意义的字段
        keep_fields = [
            "key", "itemType", "title", "abstractNote", "date", "language",
            "url", "DOI", "ISSN", "ISBN", "volume", "issue", "pages",
            "publicationTitle", "journalAbbreviation", "shortTitle",
            "creators", "tags", "extra", "libraryCatalog",
            "proceedingsTitle", "conferenceName", "publisher", "place",
            "series", "edition", "dateAdded", "dateModified",
        ]
        metadata = {k: v for k, v in data.items() if k in keep_fields and v}
        metadata["cite_key"] = cite_key

        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        updated += 1

    logger.info("=== 元数据更新完成 ===")
    if force:
        logger.info("更新: %d | 无法映射: %d | 总目录: %d | 模式: force overwrite",
                    updated, no_key, len(dirs))
    else:
        logger.info("更新: %d | 已存在(跳过): %d | 无法映射: %d | 总目录: %d",
                    updated, skipped, no_key, len(dirs))


def main():
    parser = argparse.ArgumentParser(description="MinerU Zotero PDF 批量解析")
    parser.add_argument("--dry-run", action="store_true", help="仅列出条目，不提交解析")
    parser.add_argument("--limit", type=int, default=0, help="限制处理条目数 (0=全部)")
    parser.add_argument("--config", type=str, default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--retry-failed", action="store_true", help="重试之前失败的条目")
    parser.add_argument("--item-type", choices=["journalArticle", "conferencePaper", "preprint"], default=None, help="仅处理指定类型（默认全部）")
    parser.add_argument("--update-metadata", action="store_true", help="为已解析的条目补全 Zotero 元数据")
    parser.add_argument("--force", action="store_true", help="与 --update-metadata 搭配，覆盖已存在的 metadata.json")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(SCRIPT_DIR, "parser.log"), encoding="utf-8", mode="a"
            ),
        ],
    )

    cfg = load_config(Path(args.config))
    output_dir = cfg["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    token = read_mineru_token(cfg["mineru_token_file"])
    logger.info("MinerU Token: %s...%s", token[:4], token[-4:])

    # 验证 Zotero API
    try:
        zotero_get("/items", cfg, params={"limit": 1, "format": "json"})
        logger.info("Zotero 本地 API 连接成功")
    except Exception as e:
        logger.error("Zotero 本地 API 不可达: %s (请确认 Zotero 正在运行)", e)
        sys.exit(1)

    # 构建 cite_key 到 item_key 的稳定映射
    logger.info("构建 cite_key -> item_key 映射...")
    cite_key_map = build_cite_key_to_item_key_map(cfg)
    logger.info("索引 %d 个 cite_key -> item_key 映射", len(cite_key_map))

    # 获取所有条目
    all_entries = collect_entries(cfg, cite_key_map, args.item_type)
    logger.info("共 %d 条期刊/会议论文", len(all_entries))

    if args.force and not args.update_metadata:
        logger.error("--force 只能与 --update-metadata 一起使用")
        sys.exit(2)

    # 元数据更新模式
    if args.update_metadata:
        update_metadata(cfg, output_dir, all_entries, force=args.force)
        return

    # 解析 PDF
    pdf_tasks = []
    skipped = 0
    no_pdf = 0

    for i, entry in enumerate(all_entries):
        cite_key = entry["cite_key"]

        # 检查是否已完成（以 full.md 为标志，避免被 metadata.json 误判）
        dest_dir = os.path.join(output_dir, cite_key)
        if os.path.isfile(os.path.join(dest_dir, "full.md")):
            skipped += 1
            continue

        if args.limit > 0 and len(pdf_tasks) >= args.limit:
            break

        logger.info("[%d/%d] 解析附件: %s", i + 1, len(all_entries), cite_key)
        pdf_info = resolve_pdf(entry, cfg)
        if pdf_info is None:
            logger.info("  跳过 (无可用 PDF): %s", cite_key)
            no_pdf += 1
            continue

        # 先抓元数据（几乎必定成功）
        meta_file = os.path.join(dest_dir, "metadata.json")
        if not os.path.exists(meta_file) and entry.get("item_key"):
            os.makedirs(dest_dir, exist_ok=True)
            data = fetch_item_metadata(cfg, entry["item_key"])
            if data:
                keep_fields = [
                    "key", "itemType", "title", "abstractNote", "date", "language",
                    "url", "DOI", "ISSN", "ISBN", "volume", "issue", "pages",
                    "publicationTitle", "journalAbbreviation", "shortTitle",
                    "creators", "tags", "extra", "libraryCatalog",
                    "proceedingsTitle", "conferenceName", "publisher", "place",
                    "series", "edition", "dateAdded", "dateModified",
                ]
                metadata = {k: v for k, v in data.items() if k in keep_fields and v}
                metadata["cite_key"] = cite_key
                with open(meta_file, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2, ensure_ascii=False)
                logger.info("  元数据已保存: %s", cite_key)

        pdf_tasks.append(pdf_info)

    logger.info("待处理: %d | 已跳过(已完成): %d | 无 PDF: %d", len(pdf_tasks), skipped, no_pdf)

    if args.dry_run:
        print("\n=== DRY RUN ===")
        print(f"待处理: {len(pdf_tasks)} 个条目\n")
        for t in pdf_tasks:
            print(f"  {t['cite_key']}: {t['local_path']}")
        return

    if not pdf_tasks:
        logger.info("没有需要处理的条目")
        return

    # 分批处理
    progress = load_progress(output_dir)
    if args.retry_failed:
        failed_keys = set(progress.get("failed", {}).keys())
        pdf_tasks = [t for t in pdf_tasks if t["cite_key"] in failed_keys]
        logger.info("重试 %d 个失败条目", len(pdf_tasks))
        if not pdf_tasks:
            return

    batch_size = cfg.get("batch_size", 10)
    total_batches = (len(pdf_tasks) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        batch = pdf_tasks[batch_idx * batch_size : (batch_idx + 1) * batch_size]
        logger.info("=== 批次 %d/%d (%d 个文件) ===", batch_idx + 1, total_batches, len(batch))
        process_batch(token, batch, cfg, progress, output_dir)
        save_progress(output_dir, progress)

    # 汇总
    logger.info("=== 完成 ===")
    logger.info("成功: %d", len(progress.get("processed", {})))
    logger.info("失败: %d", len(progress.get("failed", {})))
    logger.info("跳过: %d", len(progress.get("skipped", {})))


if __name__ == "__main__":
    main()
