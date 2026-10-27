"""
节点 4: llm_reasoning_node（灰区推理）
职责：规则引擎无法直接定论的「冲突场景」由 LLM 综合判断。

典型冲突场景：
    - 已签收但已超出无理由期限 → 能否降级为换货 / 维修
    - 商品属于特殊品类 → 是否有例外情形
    - 政策可退但规则给出了 reject → 有没有替代方案

约束（防止 LLM 乱答应）：
    1. 只能依据【命中政策】作答，政策未覆盖则 need_human=true
    2. 不允许回到 policy_rules_node（图中没有这条边），避免规则↔LLM 死循环
    3. 最多补充 2 项信息，避免反复反问
    4. 输出必须是结构化 JSON，由 Pydantic 约束

输入：intent / facts / order_facts / policy_text / rule_result / conflict_type
输出：decision / policy_basis / need_more_info / missing_slots / need_human
"""
import json

from pydantic import BaseModel, Field

from app.agent.node.after_sales.common import format_order_facts, structured_call
from app.agent.node.after_sales.schema import (
    CONFLICT_TYPES,
    INTENT_LABELS,
    MAX_CLARIFY,
    ORDER_STATUS_MAP,
    SLOT_LABELS,
    freight_note,
)


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
5. 已签收、无质量问题、且超出七天无理由期限的，考虑降级为换货（15 天内）或维修，而不是直接拒绝
6. 需要用户补充的信息最多列 2 项，且**只列用户尚未提供、且用户能够提供的信息**；
   订单号、签收天数、金额等系统已查到的信息不要列进来
7. clause_ids 必须来自【命中政策】中的条款编号

【输出格式】
只输出一个 json 对象，不要输出任何其他文字、解释或 Markdown 代码块：
{{"action": "approve_return|approve_exchange|approve_repair|reject|consult",
"refund_amount": 0, "freight_compensation": 0, "reason": "",
"clause_ids": [], "need_more_info": false, "missing_info": [], "need_human": false}}"""


def _format_user_facts(facts: dict) -> str:
    if not facts:
        return "（无）"
    lines = [
        f"{SLOT_LABELS.get(k, k)}：{v}"
        for k, v in facts.items()
        if k != "order_id"
    ]
    return "\n".join(lines) if lines else "（无）"


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
        f"\n【订单事实】\n{format_order_facts(order_facts)}"
        f"\n【用户口述信息】\n{_format_user_facts(facts)}"
        f"\n【规则引擎判定】{json.dumps(rule_summary, ensure_ascii=False)}"
        f"\n【命中政策】\n{state.get('policy_text') or '（知识库未覆盖）'}"
    )

    result = structured_call(ReasoningSchema, prompt)

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
