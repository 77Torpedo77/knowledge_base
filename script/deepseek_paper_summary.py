import json
import argparse
import os
import time
from pathlib import Path
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "script" / "config.json"
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "after_ds_data"

SYSTEM_PROMPT = """You are an academic paper analysis assistant. The user will provide the full text of a paper. It may be a standard research paper, a review/survey paper, or a technical overview article. First identify the paper type, then extract information accordingly and output strictly in JSON format.

The JSON must contain these fields:
{
    "title": "论文题目",
    "abstract": "摘要（提取原文中摘要性质的内容。若原文有明确的 Abstract 段落则完整提取；若没有明确标题，但某段内容在语义上起到摘要作用（如概述全文目的、方法和结论），则将该段内容作为摘要提取。若原文中确实不存在任何摘要性质的段落，则填"在原文中无法找到"）",
    "background": "问题背景（研究动机、问题定义、或该领域的现状与挑战）",
    "related_work": "相关工作（前人研究综述、历史发展脉络、或文中引用的关键先前工作）",
    "methodology": "本文方法（提出的方法/算法/模型/技术方案的核心细节；若为综述文章则提取文中讨论的核心技术分类与框架）",
    "datasets": "实验数据集（使用了哪些数据集；若文中未涉及实验则填"在原文中无法找到"）",
    "baselines": "实验对比方法（列出文中所有被用作对比的方法，不可遗漏任何一处提到的对比方法；若无对比实验则填"在原文中无法找到"）",
    "results": "实验结果（主要实验结论和关键定量数据；若文中未涉及实验则填"在原文中无法找到"）",
    "conclusion": "本文结论（总结、贡献与未来方向）"
}

IMPORTANT RULES:
- NEVER fabricate or infer content that does not exist in the original text. Only extract what is explicitly present in the original text.
- You may use semantic judgment to identify a section (e.g., recognizing a paragraph as an abstract based on its content), but you must NOT generate new text or summarize from other parts to fill in a missing section.
- For "baselines": list ALL methods that are compared against anywhere in the paper, not just the most prominent one.
- If you cannot find the corresponding section in the original text, fill the field with "在原文中无法找到".
- Output must be valid JSON only, no extra text.
- For "results", include key quantitative numbers when available.
- For "datasets" and "baselines", list all that are mentioned.
"""


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def find_paper_dirs(data_dir: Path, limit: int | None = None):
    dirs = sorted(
        d for d in data_dir.iterdir()
        if d.is_dir() and (d / "full.md").exists()
    )
    if limit is not None:
        dirs = dirs[:limit]
    return dirs


def summarize_paper(client: OpenAI, paper_dir: Path) -> dict | None:
    md_path = paper_dir / "full.md"
    text = md_path.read_text(encoding="utf-8")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Please analyze the following paper:\n\n{text}"},
    ]

    try:
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=4096,
        )
        content = response.choices[0].message.content
        if not content or content.strip() == "":
            print(f"  [WARN] Empty response for {paper_dir.name}")
            return None
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"  [ERROR] JSON parse failed for {paper_dir.name}: {e}")
        return None
    except Exception as e:
        print(f"  [ERROR] API call failed for {paper_dir.name}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Summarize papers via DeepSeek JSON Output")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of papers to process")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between API calls in seconds (default: 1.0)")
    args = parser.parse_args()

    config = load_config()
    api_key = config.get("llm_key")
    if not api_key:
        print("ERROR: 'llm_key' not found in config.json")
        return

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    paper_dirs = find_paper_dirs(DATA_DIR, args.limit)
    print(f"Found {len(paper_dirs)} papers to process")

    success_count = 0
    for i, paper_dir in enumerate(paper_dirs, 1):
        print(f"[{i}/{len(paper_dirs)}] Processing: {paper_dir.name}")
        summary = summarize_paper(client, paper_dir)
        if summary:
            out_file = OUTPUT_DIR / f"{paper_dir.name}.json"
            out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            success_count += 1
        if i < len(paper_dirs):
            time.sleep(args.delay)

    print(f"\nDone. {success_count}/{len(paper_dirs)} summaries saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
