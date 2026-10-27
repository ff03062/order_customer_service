"""
节点 1: intent_slot_node
职责：售后二级意图识别 + 槽位抽取（一次 LLM 调用同时完成）

输入：user_input / chat_history / last_order_id
输出：intent / intent_confidence / facts / missing_slots / need_clarify / rewritten_input
"""
import re

from pydantic import BaseModel, Field

from app.agent.node.after_sales.common import structured_call
from app.agent.node.after_sales.schema import (
    INTENT_LABELS,
    POLICY_ONLY_INTENTS,
    ORDER_STATUS_MAP,
    AFTER_SALES_TYPE_MAP,
    AFTER_SALES_STATUS_MAP,
    LOGISTICS_STATUS_MAP,
    missing_required_slots,
)
from app.agent.node.order_query.node.query_rewrite_node import rewrite_question

# 置信度阈值：低于此值认为意图不明确，触发反问
CONFIDENCE_THRESHOLD = 0.7


class FactsSchema(BaseModel):
    """从用户口述中抽取的事实（数据库查不到的必须问用户）。"""

    reason: str = Field(default="", description="退货/换货原因，未提及留空")
    damage_desc: str = Field(default="", description="商品损坏或故障的具体描述，未提及留空")
    quality_issue: bool = Field(default=False, description="用户是否明确表示存在质量问题")


class AfterSalesIntentSchema(BaseModel):
    """售后意图 + 槽位 的结构化输出。"""

    intent: str = Field(description="售后二级意图，必须是给定枚举值之一")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="意图判断置信度 0~1")
    order_id: str = Field(default="", description="用户本次明确提到的订单号，未提及留空")
    facts: FactsSchema = Field(default_factory=FactsSchema, description="抽取到的事实")


_SYSTEM_PROMPT = """你是电商平台的「售后意图分析助手」，负责判断用户的售后诉求并抽取关键信息。

【售后意图枚举（必须从中选择一个）】
{options}

【业务状态码参考】
订单状态：{order_status_map}
售后类型：{after_sales_type_map}
售后处理状态：{after_sales_status_map}
物流状态：{logistics_status_map}

【抽取规则】
1. intent 必须严格从枚举中选择，不要创造新值
2. confidence 表示你对 intent 判断的把握程度，信息不足时给低分（< 0.7）
3. order_id 只填用户**本次明确说出**的订单号；如果用户说的是"这个订单""那笔"，留空
4. quality_issue 只有在用户明确描述质量问题时才为 true（如"衣服破了""手机开不了机"）
5. 只根据用户明确表达的内容填写，不要推测、不要编造

【输出格式】
只输出一个 json 对象，不要输出任何其他文字、解释或 Markdown 代码块：
{{"intent": "...", "confidence": 0.0, "order_id": "", "facts": {{"reason": "", "damage_desc": "", "quality_issue": false}}}}"""

# ===== 关键词兜底 =====
# 用户回答反问时常常只回一句很短的短语（"有质量问题"），
# 模型偶尔抽不出来，会导致同一个问题被反复追问 —— 这里用关键词兜底补上。
QUALITY_KEYWORDS = [
    "质量问题", "坏了", "破损", "损坏", "故障", "开裂",
    "漏", "变形", "瑕疵", "划痕", "污渍", "开不了机", "不能用",
]
REASON_KEYWORDS = [
    "尺码", "尺寸", "不合适", "偏大", "偏小",
    "不想要", "不喜欢", "不需要", "买错", "拍错",
    "发错", "少发", "漏发", "与描述不符", "色差",
]
# 只保留明确表示否定的词。原来还放了 "没"/"无"/"非"，
# 它们是别的前缀，会把"无所谓""非常满意"误判成否定，反而抽错事实。
NEGATION_HINTS = ["没有", "不是", "并非"]


def _fallback_facts(user_input: str, facts: dict) -> dict:
    """模型没抽出来的字段，用关键词兜底补齐（只补，不覆盖模型结果）。"""
    text = user_input or ""
    if not text:
        return facts

    negated = any(hint in text for hint in NEGATION_HINTS)
    hit_quality = any(kw in text for kw in QUALITY_KEYWORDS)

    if not facts.get("quality_issue") and not negated and hit_quality:
        facts["quality_issue"] = True

    if not facts.get("damage_desc") and not negated and hit_quality:
        facts["damage_desc"] = text[:50]

    if not facts.get("reason"):
        if facts.get("quality_issue"):
            facts["reason"] = "质量问题"
        elif any(kw in text for kw in REASON_KEYWORDS):
            facts["reason"] = text[:50]

    return facts


def _format_history(chat_history) -> str:
    """把最近几轮消息格式化成纯文本。"""
    if not chat_history:
        return "（无）"
    lines = []
    for msg in chat_history[-6:]:
        content = getattr(msg, "content", None)
        if not content or not isinstance(content, str):
            continue
        role = "用户" if getattr(msg, "type", "") == "human" else "客服"
        lines.append(f"{role}：{content[:200]}")
    return "\n".join(lines) if lines else "（无）"


def _extract_facts(data: FactsSchema) -> dict:
    """把 Pydantic 模型转成紧凑的 facts 字典（空值不写入）。"""
    facts = {}
    if data.reason:
        facts["reason"] = data.reason
    if data.damage_desc:
        facts["damage_desc"] = data.damage_desc
    if data.quality_issue:
        facts["quality_issue"] = True
    return facts


def intent_slot_node(state: dict) -> dict:
    """售后意图识别 + 槽位抽取。"""
    user_input = (state.get("user_input") or "").strip()
    chat_history = state.get("chat_history") or []
    last_order_id = str(state.get("last_order_id") or "")

    # 指代消解："那我要退货" → "订单 xxx 我要退货"
    rewritten = rewrite_question(
        user_input=user_input,
        chat_history=chat_history,
        last_order_id=last_order_id,
        order_context=state.get("order_context", ""),
    )

    # 意图和映射码
    system = _SYSTEM_PROMPT.format(
        options=" | ".join(f"{k}（{v}）" for k, v in INTENT_LABELS.items()),
        order_status_map=ORDER_STATUS_MAP,
        after_sales_type_map=AFTER_SALES_TYPE_MAP,
        after_sales_status_map=AFTER_SALES_STATUS_MAP,
        logistics_status_map=LOGISTICS_STATUS_MAP,
    )
    raw = structured_call(
        AfterSalesIntentSchema,
        f"{system}\n\n【用户当前问题】{user_input}\n【最近对话】\n{_format_history(chat_history)}",
    )

    facts: dict = {}

    if raw is not None:
        intent = raw.intent if raw.intent in INTENT_LABELS else "special_goods_policy"
        confidence = float(raw.confidence or 0.0)
        facts = _extract_facts(raw.facts)

        # 模型常把"3号订单"整串当订单号返回，这里只取其中的数字，否则查库一定失败
        digits = re.findall(r"\d+", str(raw.order_id or ""))
        if digits:
            facts["order_id"] = digits[0]

    # ---- order_id 优先级：本轮明说 > 重写后的问题 > 上一轮上下文 ----
    # 这块的逻辑作ord_id确认无误，降级确认
    if not facts.get("order_id"):
        matched = re.findall(r"\d+", rewritten or user_input)
        if matched:
            facts["order_id"] = matched[0]
    if not facts.get("order_id") and last_order_id:
        facts["order_id"] = last_order_id

    # ---- 关键词兜底：模型没抽出来的字段（尤其是用户只回一句短话时）----
    facts = _fallback_facts(user_input, facts)

    # ---- 槽位完整性 ----
    missing = missing_required_slots(intent, facts)
    # 纯政策咨询不需要订单号，不因缺订单号而反问
    policy_only = intent in POLICY_ONLY_INTENTS
    need_clarify = (not policy_only) and (
            bool(missing) or confidence < CONFIDENCE_THRESHOLD
    )

    print(
        f"[intent_slot] intent={intent} conf={confidence:.2f} "
        f"facts={facts} missing={missing} policy_only={policy_only}"
    )

    return {
        "intent": intent,
        "intent_confidence": confidence,
        "rewritten_input": rewritten,
        "facts": facts,
        "missing_slots": missing,
        "need_clarify": need_clarify,
        "clarify_count": state.get("clarify_count", 0),
    }
