"""
售后政策检索工具。

向量检索 + BM25 + RRF 融合排序，返回最相关的政策片段。
文档来源：data/policies/after_sales_policy.md（写死在代码里）
"""
import os
import jieba
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv

load_dotenv()

POLICY_DOCS = [
    "自收到商品之日起7天内可申请退货，15天内可申请换货。退货时需保持商品完好、附件齐全、发票完整。",
    '退货流程：1. 在订单详情页点击"申请退货" 2. 选择退货原因并上传凭证 3. 客服审核通过后寄回商品 4. 收到退货并确认后3-5个工作日内退款。',
    "退款规则：收到退货并确认后3-5个工作日内原路返回。退款方式：原支付渠道退回（支付宝/微信/银行卡），退款时间根据支付渠道不同可能延迟1-3个工作日。特殊情况：未收货取消订单24小时内处理，部分退款联系客服申请。",
    '运费险：购买运费险后，退货时可获得最高20元的运费补偿。理赔流程：1. 退货时选择"使用运费险" 2. 填写物流单号 3. 确认收货后自动理赔 4. 理赔金额将退回至用户账户。注意：运费险仅限退货使用，换货不适用。',
    "保修服务：自购买之日起一年内在非人为损坏情况下可享受免费保修服务。保修范围：产品质量问题、非人为因素导致的故障。保修流程：1. 联系客服申请保修 2. 寄送商品至指定地址 3. 维修完成后寄回。保修凭证：需要提供订单截图和发票。",
    '售后联系方式：客服电话400-123-4567（9:00-21:00），在线客服：App内右下角"我的"-"客服中心"，邮箱：service@example.com。',
    "特殊商品政策：以下商品不支持7天无理由退货：生鲜、食品、定制商品、贴身衣物、软件、虚拟商品。以下商品在保修期内可申请换货：电子产品（手机、电脑等）、家用电器、家具。",
]

# 预加载 BM25 索引
BM25_INDEX = BM25Okapi([list(jieba.cut(doc)) for doc in POLICY_DOCS])

# 预加载词嵌入模型
EMBEDDING_MODEL = SentenceTransformer(os.getenv("EMBEDDING_MODEL_PATH"))

# 预编码文档向量（写在函数外部，只算一次）
DOC_VECTORS = [EMBEDDING_MODEL.encode([doc], normalize_embeddings=True)for doc in POLICY_DOCS]


def vector_search(query: str, top_k: int = 3) -> list[tuple[str, float]]:
    """向量检索：使用 BGE 模型计算相似度，返回 top_k 条结果。"""
    # 计算问题向量
    query_vec = EMBEDDING_MODEL.encode([query], normalize_embeddings=True)
    results = []
    for doc, doc_vec in zip(POLICY_DOCS, DOC_VECTORS):
        # 向量检索器
        # 计算问题得分
        score = float((query_vec @ doc_vec.T)[0][0])
        # 过滤掉相似度小于 0.7 的结果
        if score >= 0.7:
            results.append((doc, score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]


def bm25_search(query: str, top_k: int = 3) -> list[tuple[str, float]]:
    # BM25 检索：使用 jieba 分词，返回 top_k 条结果。
    tokenized_query = list(jieba.cut(query))
    # 由bm2检索器计算得分
    raw_scores = BM25_INDEX.get_scores(tokenized_query)
    results = []
    for doc, score in zip(POLICY_DOCS, raw_scores):
        # 过滤掉相似度小于 0.7 的结果
        if score >= 0.7:
            results.append((doc, score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]


def rrf_fusion(
    vector_results: list[tuple[str, float]],
    bm25_results: list[tuple[str, float]],
    # 固定参
    k: int = 60,
) -> list[tuple[str, float]]:
    """RRF（Reciprocal Rank Fusion）：对两条排序结果做融合。"""
    doc_rrf = {}
    for rank, (doc, _) in enumerate(vector_results, start=1):
        doc_rrf[doc] = doc_rrf.get(doc, 0.0) + 1.0 / (k + rank)
    for rank, (doc, _) in enumerate(bm25_results, start=1):
        doc_rrf[doc] = doc_rrf.get(doc, 0.0) + 1.0 / (k + rank)
    fused = [(doc, score) for doc, score in doc_rrf.items()]
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused


def search(query: str, top_k: int = 3) -> list[tuple[str, float]]:
    """检索入口：向量检索 + BM25 检索 → RRF 融合 → 返回最终排序结果。"""
    vec = vector_search(query, top_k=top_k)
    bm25 = bm25_search(query, top_k=top_k)
    if not vec and not bm25:
        return []
    fused = rrf_fusion(vec, bm25)
    return fused[:top_k]