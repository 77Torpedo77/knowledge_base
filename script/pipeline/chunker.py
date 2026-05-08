"""Phase 1: 物理切块 — 将 Markdown 按行切分为带 ID 的文本块数组"""


def chunk_markdown(text: str) -> list[dict]:
    """将 Markdown 按行切分为带 ID 的文本块数组，过滤纯空行。"""
    blocks = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            blocks.append({"id": len(blocks) + 1, "text": stripped})
    return blocks
