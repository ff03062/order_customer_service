"""
售后决策子图专用 State

与主图 CustomerServiceState、订单子图 OrderQueryState 完全独立：
子图状态生命周期 = 单次调用，调用结束即销毁。
需要跨轮保留的信息（如 order_id）由主图 state 承载，通过输入注入。
"""
from typing import TypedDict


class AfterSalesState(TypedDict):
    """
    字段流向：
        intent_slot_node        → intent / intent_confidence / need_clarify / missing_slots / facts / sub_flags
        order_facts_node        → order_facts / db_error
        policy_rules_node       → policy_clauses / rule_result / decision / conflict_detected / conflict_type / policy_basis
        llm_reasoning_node      → decision / need_human / need_more_info / missing_slots / policy_basis
        solution_generate_node  → solution / need_execute / execute_payload
        clarify_node            → clarify_question
        action_execute_node     → execute_result
    """

    # === 输入（由主图 after_sales_node 注入）===
    user_input: str
    user_id: str
    chat_history: list           # 最近几轮消息，用于指代消解（如"那我要退货"）
    last_order_id: str           # 上一轮订单号（来自主图 current_order_id）
    order_context: str           # 上一轮在聊什么

    # === 意图与槽位 ===
    intent: str                  # 二级售后意图，见 schema.INTENT_LABELS
    intent_confidence: float
    rewritten_input: str         # 指代消解后的问题
    facts: dict                  # 从对话抽取的事实（用户口述）
    missing_slots: list[str]     # 缺失的必需槽位
    need_clarify: bool           # 是否需要反问用户（由条件边读取）
    clarify_question: str        # 反问话术
    clarify_count: int           # 本次调用内反问次数（上限 MAX_CLARIFY）

    # === 订单事实（查库得到，不是用户说的）===
    order_facts: dict
    db_error: str

    # === 政策与规则 ===
    policy_clauses: list         # 命中的政策条款 [{clause_id,title,text,score}]
    policy_text: str             # 拼好的政策文本（带条款编号，直接进 Prompt）
    policy_basis: list[str]      # 决策依据的条款编号（引用给用户看）
    rule_result: dict            # {action, refund_amount, freight_compensation, reason_code, notes}
    conflict_detected: bool
    conflict_type: str           # 见 schema.CONFLICT_TYPES

    # === 决策与输出 ===
    decision: dict               # 最终决策
    need_more_info: bool
    need_human: bool             # 需转人工
    solution: str                # 给用户的最终答复

    # === 执行（MCP 桩）===
    need_execute: bool
    execute_payload: dict
    execute_result: dict
