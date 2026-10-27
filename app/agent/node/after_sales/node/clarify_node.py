"""
节点: clarify_node（反问澄清）
职责：关键信息缺失时，向用户提出追问，而不是硬猜或直接拒绝。

单次调用内最多反问 MAX_CLARIFY 次（防止死循环）；
超出上限或意图本身不明确时，走兜底话术并把问题交给人工。

输入：missing_slots / facts / clarify_count / intent
输出：clarify_question / solution / clarify_count
"""
from app.agent.node.after_sales.schema import (
    INTENT_LABELS,
    MAX_CLARIFY,
    SLOT_LABELS,
    SLOT_QUESTIONS,
)

FALLBACK_TEXT = (
    "抱歉，我这边还需要更多信息才能帮您处理这次售后。"
    "您可以在订单详情页提交售后申请，或直接致电 400-123-4567（9:00-21:00）由人工客服协助。"
)


def _build_question(state: dict) -> str:
    """按缺失情况生成反问话术（只问真正缺的、用户能提供的信息）。"""
    missing = list(state.get("missing_slots") or [])
    facts = state.get("facts") or {}
    db_error = state.get("db_error", "")

    # 订单号已经有了，就绝不能再问"是哪一笔订单"
    if facts.get("order_id"):
        missing = [slot for slot in missing if slot != "order_id"]

    # 订单查询失败：引导核对订单号
    if db_error and "不存在" in str(db_error):
        return ("抱歉，我没有查到这个订单号对应的订单，"
                "麻烦您再核对一下订单号，或者告诉我下单的手机号，我帮您查～")
    if db_error and "missing_order_id" in str(db_error) and not facts.get("order_id"):
        return SLOT_QUESTIONS["order_id"]

    if missing:
        question = SLOT_QUESTIONS.get(missing[0])
        if question:
            return question
        return f"方便补充一下「{SLOT_LABELS.get(missing[0], missing[0])}」吗？我好帮您判断～"

    # 什么都不缺：说明是模型想澄清但实际信息已够，给一个不依赖订单号的兜底问法
    label = INTENT_LABELS.get(state.get("intent", ""), "售后")
    return (f"好的，我在帮您处理「{label}」。"
            "麻烦再具体说一下您的诉求（例如：想退货、想换货、还是想维修），我马上为您判断～")


def clarify_node(state: dict) -> dict:
    clarify_count = int(state.get("clarify_count") or 0) + 1

    if clarify_count > MAX_CLARIFY:
        print(f"[clarify] 已达反问上限 {MAX_CLARIFY}，转人工兜底")
        return {
            "clarify_count": clarify_count,
            "clarify_question": "",
            "solution": FALLBACK_TEXT,
            "need_human": True,
        }

    question = _build_question(state)
    print(f"[clarify] 第 {clarify_count} 次反问：{question}")

    return {
        "clarify_count": clarify_count,
        "clarify_question": question,
        "solution": question,
    }
