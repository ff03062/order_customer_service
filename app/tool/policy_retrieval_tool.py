"""
售后政策检索工具（条款级）

改造要点（相对旧版把 7 条政策硬编码在 Python 里）：
    1. 政策来源改为 data/policies/after_sales_policy.md —— 改文档即生效，无需改代码
    2. 按 Markdown 标题切成「条款」（clause），每条带 clause_id，便于回答时引用依据
    3. 保留原有的「向量检索 + BM25 + RRF 融合」三件套，只把粒度从整篇改成条款

对外接口：
    search_clauses(query, top_k)   → list[dict]  给程序调用（结构化）
    search(query, top_k)           → list[tuple] 兼容旧调用（文本 + 分数）
    format_clauses(clauses)        → str         给 Prompt 用的文本
"""
import os
import re

import jieba
from dotenv import load_dotenv
from rank_bm25 import BM25Okapi

load_dotenv()

# 政策文档路径（app/tool/ -> ../../data/policies/）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POLICY_DOC_PATH = os.getenv(
    "POLICY_DOC_PATH",
    os.path.join(_PROJECT_ROOT, "data", "policies", "after_sales_policy.md"),
)

# 召回阈值：向量用余弦相似度（0~1，0.7 合理）；
# BM25 分数是无上界量纲，阈值必须放低，否则短查询几乎召回不到
VECTOR_THRESHOLD = 0.7
BM25_THRESHOLD = 0.3

# 懒加载缓存
_CLAUSES = None
_BM25_INDEX = None
_EMBEDDING_MODEL = None
_DOC_VECTORS = None


# ============================================================
# 一、政策文档 → 条款切分
# ============================================================

def _strip_markdown(text: str) -> str:
    """去掉 Markdown 标记，保留纯文本，便于向量化与 BM25 分词。"""
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)   # 标题符号
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                 # 粗体
    text = re.sub(r"[`*_>]", "", text)                           # 其他标记
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_clauses() -> list[dict]:
    """
    把政策 md 按标题切分成条款列表。

    切分规则：
        - `## 一级标题` 开启新条款
        - `### 二级标题` 作为该条款的子节，保留在正文中

    返回：[{"clause_id": "POLICY-01", "title": "退货政策", "text": "..."}]
    """
    global _CLAUSES
    if _CLAUSES is not None:
        return _CLAUSES

    clauses: list[dict] = []
    if not os.path.exists(POLICY_DOC_PATH):
        print(f"[policy] 政策文档不存在：{POLICY_DOC_PATH}")
        _CLAUSES = clauses
        return _CLAUSES

    with open(POLICY_DOC_PATH, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    current_title = None
    buffer: list[str] = []

    def flush():
        if current_title and buffer:
            body = _strip_markdown("\n".join(buffer))
            if body:
                clauses.append({
                    "clause_id": f"POLICY-{len(clauses) + 1:02d}",
                    "title": current_title,
                    "text": body,
                })

    for line in lines:
        if line.startswith("## "):           # 一级条款
            flush()
            current_title = line[3:].strip()
            buffer = []
        elif line.startswith("# "):          # 文档总标题，跳过
            continue
        else:
            if current_title is not None:
                buffer.append(line)
    flush()

    _CLAUSES = clauses
    print(f"[policy] 已加载政策条款 {len(clauses)} 条：{POLICY_DOC_PATH}")
    return _CLAUSES


# ============================================================
# 二、检索索引（懒加载，避免 import 时触发模型加载）
# ============================================================

def _ensure_index():
    global _BM25_INDEX, _EMBEDDING_MODEL, _DOC_VECTORS

    clauses = _load_clauses()
    if not clauses:
        return

    if _BM25_INDEX is None:
        _BM25_INDEX = BM25Okapi([list(jieba.cut(c["text"])) for c in clauses])

    if _EMBEDDING_MODEL is None:
        model_path = os.getenv("EMBEDDING_MODEL_PATH")
        try:
            from sentence_transformers import SentenceTransformer
            _EMBEDDING_MODEL = SentenceTransformer(model_path)
        except Exception as e:
            print(f"[policy] 向量模型加载失败，降级为纯 BM25 检索: {e}")
            _EMBEDDING_MODEL = False   # False 表示「已尝试但失败」

    if _DOC_VECTORS is None:
        if _EMBEDDING_MODEL:
            _DOC_VECTORS = [
                _EMBEDDING_MODEL.encode([c["text"]], normalize_embeddings=True)
                for c in clauses
            ]
        else:
            _DOC_VECTORS = []


# ============================================================
# 三、两路召回 + RRF 融合
# ============================================================

def vector_search(query: str, top_k: int = 3) -> list[tuple[str, float]]:
    """向量检索：返回 (clause_id, 相似度) 列表。"""
    _ensure_index()
    clauses = _load_clauses()
    if not _EMBEDDING_MODEL or not _DOC_VECTORS:
        return []

    query_vec = _EMBEDDING_MODEL.encode([query], normalize_embeddings=True)
    results = []
    for clause, doc_vec in zip(clauses, _DOC_VECTORS):
        score = float((query_vec @ doc_vec.T)[0][0])
        if score >= VECTOR_THRESHOLD:
            results.append((clause["clause_id"], score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]


def bm25_search(query: str, top_k: int = 3) -> list[tuple[str, float]]:
    """BM25 检索：返回 (clause_id, 分数) 列表。"""
    _ensure_index()
    clauses = _load_clauses()
    if not _BM25_INDEX:
        return []

    scores = _BM25_INDEX.get_scores(list(jieba.cut(query)))
    results = []
    for clause, score in zip(clauses, scores):
        if score >= BM25_THRESHOLD:
            results.append((clause["clause_id"], float(score)))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]


def rrf_fusion(
    vector_results: list[tuple[str, float]],
    bm25_results: list[tuple[str, float]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """RRF（Reciprocal Rank Fusion）：对两路排序结果做融合。"""
    merged: dict[str, float] = {}
    for rank, (doc_id, _) in enumerate(vector_results, start=1):
        merged[doc_id] = merged.get(doc_id, 0.0) + 1.0 / (k + rank)
    for rank, (doc_id, _) in enumerate(bm25_results, start=1):
        merged[doc_id] = merged.get(doc_id, 0.0) + 1.0 / (k + rank)
    fused = list(merged.items())
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused


# ============================================================
# 四、对外接口
# ============================================================

def search_clauses(query: str, top_k: int = 3) -> list[dict]:
    """
    条款级检索入口。

    返回：[{"clause_id", "title", "text", "score"}]
    两路都召回不到时降级为「标题字面命中」，仍无结果则返回空列表。
    """
    clauses = _load_clauses()
    if not clauses:
        return []

    by_id = {c["clause_id"]: c for c in clauses}
    vec = vector_search(query, top_k=top_k)
    bm25 = bm25_search(query, top_k=top_k)

    if not vec and not bm25:
        # 字面兜底：查询词直接出现在条款标题里
        hits = [
            c for c in clauses
            if any(tok and len(tok) > 1 and tok in c["title"] for tok in jieba.cut(query))
        ]
        return [{**c, "score": 0.0} for c in hits[:top_k]]

    fused = rrf_fusion(vec, bm25)[:top_k]
    return [
        {**by_id[doc_id], "score": round(score, 6)}
        for doc_id, score in fused
        if doc_id in by_id
    ]


def search(query: str, top_k: int = 3) -> list[tuple[str, float]]:
    """兼容旧接口：返回 [(条款纯文本, 分数)]。"""
    return [(c["text"], c["score"]) for c in search_clauses(query, top_k)]


def format_clauses(clauses: list[dict]) -> str:
    """把条款列表格式化成给 Prompt 用的文本（带编号，便于模型引用依据）。"""
    if not clauses:
        return "（知识库未覆盖该问题）"
    return "\n\n".join(
        f"[{c['clause_id']}] {c['title']}\n{c['text']}" for c in clauses
    )


if __name__ == "__main__":
    for q in ["退货几天能到账", "运费险换货能用吗", "定制商品能退吗"]:
        print("=" * 60)
        print("问题：", q)
        for c in search_clauses(q):
            print(f"  [{c['clause_id']}] {c['title']}  score={c['score']}")
