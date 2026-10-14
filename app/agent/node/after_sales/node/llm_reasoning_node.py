"""
节点 4: llm_reasoning_node（灰区推理）
职责：规则引擎无法直接定论的「冲突场景」由 LLM 综合判断。

典型冲突场景：
    - 已拆封但存在质量问题 → 包装完整性与质量责任如何权衡
    - 政策可退但订单已超出无理由期限 → 能否降级为换货 / 维修
    - 商品属于特殊品类 → 是否有例外情形
    - 物流损坏 → 责任归属判断

约束（防止 LLM 乱答应）：
    1. 只能依据【命中政策】作答，政策未覆盖则 need_human=true
    2. 不允许回到 policy_rules_node（图中没有这条边），避免规则↔LLM 死循环
    3. 最多补充 2 项信息，避免反复反问
    4. 输出必须是结构化 JSON，由 Pydantic 约束

输入：intent / facts / order_facts / policy_text / rule_result / conflict_type
输出：decision / policy_basis / need_more_info / missing_slots / need_human
"""
import json

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.agent.model.my_model import MyModel
from app.agent.node.after_sales.schema import (
    CONFLICT_TYPES,
    INTENT_LABELS,
    ORDER_STATUS_MAP,
    SLOT_LABELS,
    freight_note,
)

MAX_CLARIFY = 2   # 单次调用内反问次数上限


class ReasoningSchema(BaseModel):
    """灰区推理的结构化输出。"""

    action: str = Field(
        default="consult",
        description="结论动作：approve_return=同意退货 / approve_exchange=同意换货 / "
                    "approve_repair=同意维修 / reject=不支持 / consult=需人工确认",
    )
    refund_amount: float = Field(default=0.0, description="退款金额，无则填 0")
    freight_compensation: float = Field(default=0.0, description="运费补偿金额，无则填 0")
    reason: str = Field(default="", description="判定理由，一句话")
    clause_ids: list[str] = Field(default_factory=list, description="引用的政策条款编号")
    need_more_info: bool = Field(default=False, description="是否还需要用户补充信息")
    missing_info: list[str] = Field(default_factory=list, description="还缺哪些信息，最多 2 项")
    need_human: bool = Field(default=False, description="政策未覆盖或无法判断时转人工")


_SYSTEM_PROMPT = """你是电商平台的「售后政策裁决助手」，负责处理规则引擎无法直接定论的灰区场景。

【业务状态码】
订单状态：{order_status_map}

【冲突类型】
{conflict_desc}

【判定原则】
1. 只能依据下方【命中政策】条款作答，不得引入政策之外的信息
2. 若政策未覆盖该场景，输出 need_human=true，不要自行给出承诺
3. **未签收的订单可以随时退货**（商品在途中，七天无理由时效尚未开始），不得以超期为由拒绝
4. **质量问题不受七天无理由时效限制**：签收超过 7 天但属质量问题的，仍应支持退货
5. 质量问题通常优先于包装完整性（已拆封但有质量问题的，一般应支持售后）
6. 已签收、无质量问题、且超出七天无理由期限的，考虑降级为换货（15 天内）或维修，而不是直接拒绝
7. 需要用户补充的信息最多列 2 项，且**只列用户尚未提供、且用户能够提供的信息**；
   订单号、签收天数、金额等系统已查到的信息不要列进来
8. clause_ids 必须来自【命中政策】中的条款编号

【输出格式】
只输出一个 json 对象，不要输出任何其他文字、解释或 Markdown 代码块：
{{"action": "approve_return|approve_exchange|approve_repair|reject|consult",
"refund_amount": 0, "freight_compensation": 0, "reason": "",
"clause_ids": [], "need_more_info": false, "missing_info": [], "need_human": false}}"""


def _format_facts(order_facts: dict) -> str:
    if not order_facts or not order_facts.get("found"):
        return "（未取到订单事实）"

    is_signed = bool(order_facts.get("is_signed"))
    if is_signed:
        signed_desc = (
            f"已签收，签收距今 {order_facts.get('days_since_receipt')} 天"
            f"（依据：{order_facts.get('signed_source') or '未知'}）"
        )
    else:
        signed_desc = (
            f"未签收（物流状态 {order_facts.get('logistics_status')}）"
            "—— 商品在途中，七天无理由时效尚未开始"
        )

    return (
        f"订单状态：{order_facts.get('order_status')}"
        f"（{ORDER_STATUS_MAP}）\n"
        f"签收情况：{signed_desc}\n"
        f"下单距今：{order_facts.get('days_since_purchase')} 天\n"
        f"商品品类：{order_facts.get('categories')}\n"
        f"实付金额：{order_facts.get('pay_amount')}\n"
        f"运费险：{'有' if order_facts.get('has_freight_insurance') else '无'}\n"
        f"已有售后单：{'是' if order_facts.get('has_existing_after_sales') else '否'}"
    )


def _format_user_facts(facts: dict) -> str:
    if not facts:
        return "（无）"
    lines = [
        f"{SLOT_LABELS.get(k, k)}：{v}"
        for k, v in facts.items()
        if k != "order_id"
    ]
    return "\n".join(lines) if lines else "（无）"


def _invoke(prompt: str) -> ReasoningSchema | None:
    """结构化输出优先，失败退回文本 + JSON 解析；云端失败降级本地模型。"""
    for getter in ("get_model", "get_local_model"):
        try:
            model = getattr(MyModel, getter)()
        except Exception as e:
            print(f"[llm_reasoning] {getter} 初始化失败: {e}")
            continue

        try:
            structured = model.with_structured_output(ReasoningSchema, method="json_mode")
            result = structured.invoke([HumanMessage(content=prompt)])
            parsed = _coerce_result(result)
            if parsed is not None:
                return parsed
        except Exception as e:
            print(f"[llm_reasoning] {getter} 结构化输出失败，改用文本解析: {e}")

        try:
            text = model.invoke([HumanMessage(content=prompt)]).content
            data = _extract_json(text)
            if data:
                return ReasoningSchema(**data)
        except Exception as e:
            print(f"[llm_reasoning] {getter} 文本解析失败: {e}")

    return None


def _coerce_result(result) -> ReasoningSchema | None:
    """
    把模型返回值统一成 ReasoningSchema。

    实测云端模型即使调用 with_structured_output 也会返回 [ {...} ]（JSON 数组），
    需要先剥掉外层列表，否则报 "Input should be an object"。
    """
    if result is None:
        return None
    if isinstance(result, ReasoningSchema):
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
            return ReasoningSchema(**payload)
        except Exception as e:
            print(f"[llm_reasoning] 结构化结果字段校验失败: {e}")
    return None


def _extract_json(text: str) -> dict | None:
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


def llm_reasoning_node(state: dict) -> dict:
    order_facts = state.get("order_facts") or {}
    facts = state.get("facts") or {}
    rule_summary = (state.get("rule_result") or {}).get("summary") or {}
    conflict_type = state.get("conflict_type") or "other"
    intent = state.get("intent", "")

    prompt = _SYSTEM_PROMPT.format(
        order_status_map=ORDER_STATUS_MAP,
        conflict_desc=CONFLICT_TYPES.get(conflict_type, CONFLICT_TYPES["other"]),
    ) + (
        f"\n\n【用户诉求】{INTENT_LABELS.get(intent, intent)}"
        f"\n【用户原话】{state.get('rewritten_input') or state.get('user_input', '')}"
        f"\n【订单事实】\n{_format_facts(order_facts)}"
        f"\n【用户口述信息】\n{_format_user_facts(facts)}"
        f"\n【规则引擎判定】{json.dumps(rule_summary, ensure_ascii=False)}"
        f"\n【命中政策】\n{state.get('policy_text') or '（知识库未覆盖）'}"
    )

    result = _invoke(prompt)

    if result is None:
        # 模型不可用：保守兜底 —— 转人工，不给错误承诺
        print("[llm_reasoning] 模型不可用，转人工兜底")
        return {
            "decision": {
                **(state.get("decision") or {}),
                "action": "consult",
                "notes": (state.get("decision") or {}).get("notes", []) + ["灰区推理失败，转人工"],
            },
            "need_human": True,
            "need_more_info": False,
        }

    # 只接受政策里真实存在的条款编号（防模型编造依据）
    valid_ids = {c["clause_id"] for c in (state.get("policy_clauses") or [])}
    clause_ids = [cid for cid in (result.clause_ids or []) if cid in valid_ids]
    basis = clause_ids or (state.get("policy_basis") or [])[:3]

    # 补充信息需求也要受反问次数限制
    clarify_count = state.get("clarify_count", 0)
    need_more_info = bool(result.need_more_info) and clarify_count < MAX_CLARIFY
    missing_slots = list(result.missing_info or [])[:2] if need_more_info else []

    decision = {
        **(state.get("decision") or {}),
        "action": result.action or "consult",
        "refund_amount": float(result.refund_amount or 0.0),
        "freight_compensation": float(result.freight_compensation or 0.0),
        "reason": result.reason,
        "resolved_by": "llm_reasoning",
        "notes": (state.get("decision") or {}).get("notes", [])
        + ([result.reason] if result.reason else []),
    }
    # action 可能被 LLM 改写（如 reject → approve_exchange），
    # 运费承担说明要按新 action 重算，不能沿用旧结论
    decision["freight_note"] = freight_note(state, decision)

    print(
        f"[llm_reasoning] action={decision['action']} need_human={result.need_human} "
        f"need_more_info={need_more_info} basis={basis}"
    )

    return {
        "decision": decision,
        "policy_basis": basis,
        "need_human": bool(result.need_human),
        "need_more_info": need_more_info,
        "missing_slots": missing_slots,
    }
