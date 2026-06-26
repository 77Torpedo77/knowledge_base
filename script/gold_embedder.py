#!/usr/bin/env python3
"""
Gold Layer 向量嵌入模块。
使用 all-MiniLM-L6-v2 将 CanonicalEntity 名称嵌入为 384 维向量，
支持 Top-K 余弦相似度搜索。
"""

from __future__ import annotations

import logging
import numpy as np
from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

MODEL_PATH = r"D:\my_weight\all-MiniLM-L6-v2"


class Embedder:
    def __init__(self, model_path: str = MODEL_PATH):
        self.model = SentenceTransformer(model_path)
        self.dim = self.model.get_embedding_dimension()
        log.info("Embedder loaded: %s (%d dim)", model_path, self.dim)

    def embed(self, texts: list[str]) -> np.ndarray:
        """批量嵌入，返回 (N, dim) 的归一化 numpy 数组。"""
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        vecs = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)

    def embed_single(self, text: str) -> np.ndarray:
        """嵌入单个文本。"""
        return self.embed([text])[0]

    def search_top_k(
        self,
        query_vec: np.ndarray,
        candidates: list[dict],
        top_k: int = 5,
    ) -> list[dict]:
        """
        在候选列表中找到与 query_vec 最相似的 top_k 个。
        candidates: [{"canonical_id": ..., "canonical_name": ..., "embedding": [float, ...], ...}, ...]
        返回带 similarity 的候选列表。
        """
        if not candidates:
            return []

        candidate_vecs = np.asarray([c["embedding"] for c in candidates], dtype=np.float32)
        # 归一化后余弦相似度 = 点积
        sims = np.dot(candidate_vecs, query_vec)

        top_indices = np.argsort(sims)[::-1][:top_k]

        results = []
        for idx in top_indices:
            c = dict(candidates[idx])
            c["similarity"] = float(sims[idx])
            results.append(c)
        return results
