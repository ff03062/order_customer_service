"""
节点 1: intent_slot_node
职责：售后二级意图识别 + 槽位抽取（一次 LLM 调用同时完成）

输入：user_input / chat_history / last_order_id
输出：intent / intent_confidence / facts / missing_slots / need_clarify / rewritten_input

设计要点：
    - 用结构化输出（Pydantic）约束模型，避免解析自然语言
    - 置信度低于阈值即走反问链路，不硬猜
    - order_id 的取值优先级：本轮明说 > 上一轮上下文 > 触发反问
"""
import json
import re

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.agent.model.my_model import MyModel
from app.agent.node.order_query.node.query_rewrite_node import rewrite_question
from app.agent.node.after_sales.schema import (
    INTENT_OPTIONS,
    INTENT_LABELS,
    POLICY_ONLY_INTENTS,
    ORDER_STATUS_MAP,
    AFTER_SALES_TYPE_MAP,
    AFTER_SALES_STATUS_MAP,
    LOGISTICS_STATUS_MAP,
    missing_required_slots,
)

# 置信度阈值：低于此值认为意图不明确，触发反问
CONFIDENCE_THRESHOLD = 0.7


class FactsSchema(BaseModel):
    """从用户口述中抽取的事实（数据库查不到的必须问用户）。"""

    reason: str = Field(default="", description="退货/换货原因，未提及留空")
    damage_desc: str = Field(default="", description="商品损坏或故障的具体描述，未提及留空")
    quality_issue: bool = Field(default=False, description="用户是否明确表示存在质量问题")
    unboxed: bool = Field(default=False, description="用户是否表示商品已拆封使用")
    invoice: bool = Field(default=False, description="用户是否表示保留有发票")


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
5. unboxed 表示用户是否已拆封使用
6. 只根据用户明确表达的内容填写，不要推测、不要编造

【输出格式】
只输出一个 json 对象，不要输出任何其他文字、解释或 Markdown 代码块：
{{"intent": "...", "confidence": 0.0, "order_id": "", "facts": {{"reason": "", "damage_desc": "", "quality_issue": false, "unboxed": false, "invoice": false}}}}"""


# ===== 关键词兜底 =====
# 用户回答反问时常常只回一句很短的短语（"有质量问题"），
# 模型偶尔抽不出来，会导致同一个问题被反复追问 —— 这里用关键词兜底补上。
QUALITY_KEYWORDS = ["质量问题", "坏了", "破损", "损坏", "坏了", "故障", "开裂", "漏", "变形", "瑕疵", "开不了机", "不能用"]
NEGATION_HINTS = ["没有", "没", "不是", "无", "非"]
UNBOXED_KEYWORDS = ["拆封", "拆开", "拆了", "已拆", "打开过", "用过了", "用过了", "穿过", "试过"]
REASON_KEYWORDS = [
    "质量问题", "坏了", "破损", "损坏", "故障", "瑕疵",
    "尺码", "尺寸", "不合适", "偏大", "偏小",
    "不想要", "不喜欢", "不需要", "买错", "拍错",
    "发错", "少发", "漏发", "与描述不符", "色差",
]
DAMAGE_KEYWORDS = ["破损", "损坏", "坏了", "故障", "开裂", "漏", "变形", "瑕疵", "划痕", "污渍"]
QUALITY_REASON_TEXT = "质量问题"


def _has_negation(text: str) -> bool:
    return any(hint in text for hint in NEGATION_HINTS)


def _fallback_facts(user_input: str, facts: dict) -> dict:
    """模型没抽出来的字段，用关键词兜底补齐（只补，不覆盖模型结果）。"""
    text = user_input or ""
    if not text:
        return facts

    negated = _has_negation(text)

    if not facts.get("quality_issue") and not negated:
        if any(kw in text for kw in QUALITY_KEYWORDS):
            facts["quality_issue"] = True

    if not facts.get("unboxed") and not negated:
        if any(kw in text for kw in UNBOXED_KEYWORDS):
            facts["unboxed"] = True

    if not facts.get("damage_desc") and not negated:
        for kw in DAMAGE_KEYWORDS:
            if kw in text:
                facts["damage_desc"] = text[:50]
                break

    if not facts.get("reason"):
        if facts.get("quality_issue"):
            facts["reason"] = QUALITY_REASON_TEXT
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
    if data.unboxed:
        facts["unboxed"] = True
    if data.invoice:
        facts["invoice"] = True
    return facts


def _analyze(prompt: str) -> AfterSalesIntentSchema | None:
    """
    调用模型并返回结构化结果。

    两级兜底：
        1. 优先 with_structured_output（约束解码，最稳）
        2. 该模型不支持时，退回「纯文本 + JSON 解析」
    另外云端模型失败会自动降级到本地模型。
    """
    system = _SYSTEM_PROMPT.format(
        options=" | ".join(f"{k}（{INTENT_LABELS[k]}）" for k in INTENT_OPTIONS),
        order_status_map=ORDER_STATUS_MAP,
        after_sales_type_map=AFTER_SALES_TYPE_MAP,
        after_sales_status_map=AFTER_SALES_STATUS_MAP,
        logistics_status_map=LOGISTICS_STATUS_MAP,
    )
    full_prompt = f"{system}\n\n{prompt}"

    for getter in ("get_model", "get_local_model"):
        try:
            model = getattr(MyModel, getter)()
        except Exception as e:
            print(f"[intent_slot] {getter} 初始化失败: {e}")
            continue

        # 优先结构化输出（json_mode：让接口直接返回 JSON，避免工具调用链路）
        try:
            structured = model.with_structured_output(AfterSalesIntentSchema, method="json_mode")
            result = structured.invoke([HumanMessage(content=full_prompt)])
            parsed = _coerce_result(result)
            if parsed is not None:
                return parsed
        except Exception as e:
            print(f"[intent_slot] {getter} 结构化输出失败，改用文本解析: {e}")

        # 兜底：纯文本 + JSON 解析（实测云端模型在这条路径上稳定）
        try:
            text = model.invoke([HumanMessage(content=full_prompt)]).content
            data = _extract_json(text)
            if data:
                return AfterSalesIntentSchema(**data)
        except Exception as e:
            print(f"[intent_slot] {getter} 文本解析也失败: {e}")

    return None


def _coerce_result(result) -> AfterSalesIntentSchema | None:
    """
    把模型返回值统一成 AfterSalesIntentSchema。

    实测本项目的云端模型（OpenAI 兼容接口）即使调用 with_structured_output，
    也会把结果包成 [ {...} ]（JSON 数组）返回 —— 直接
    AfterSalesIntentSchema(**list) 会报
    "Input should be an object [type=model_type]"。
    这里统一做「剥壳」：list → 取首个 dict；dict → 兼容 result/output/data 再包一层。
    """
    if result is None:
        return None
    if isinstance(result, AfterSalesIntentSchema):
        return result

    payload = result
    if isinstance(payload, str):
        payload = _extract_json(payload)
    if isinstance(payload, list):
        payload = next((item for item in payload if isinstance(item, dict)), None)
    if isinstance(payload, dict):
        for key in ("result", "output", "data"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                payload = inner
                break
        try:
            return AfterSalesIntentSchema(**payload)
        except Exception as e:
            print(f"[intent_slot] 结构化结果字段校验失败: {e}")
    return None


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象。"""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


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

    history_text = _format_history(chat_history)
    raw = _analyze(f"【用户当前问题】{user_input}\n【最近对话】\n{history_text}")

    intent = "special_goods_policy"
    confidence = 0.0
    facts: dict = {}

    if raw is not None:
        intent = raw.intent if raw.intent in INTENT_OPTIONS else "special_goods_policy"
        confidence = float(raw.confidence or 0.0)
        facts = _extract_facts(raw.facts)
        if raw.order_id:
            facts["order_id"] = str(raw.order_id).strip()

    # ---- order_id 优先级：本轮明说 > 重写后的问题 > 上一轮上下文 ----
    if not facts.get("order_id"):
        matched = re.findall(r"\d{6,}", rewritten or user_input)
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
    if policy_only:
        need_clarify = False
    else:
        need_clarify = bool(missing) or confidence < CONFIDENCE_THRESHOLD
        # 意图不明确但有其他必需槽位时，按缺槽位反问
        if confidence < CONFIDENCE_THRESHOLD and not missing:
            missing = ["order_id"] if not facts.get("order_id") else []

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
