"""
售后决策节点 - Wrapper

职责：
    1. 把主图 state 转成子图入参（含跨轮上下文）
    2. 调子图（异步）
    3. **执行真实的外部动作**：退款 / 收运费走支付宝 MCP
    4. 把子图结果转回主图 state 字段

为什么支付宝调用放在这里，而不是子图的执行节点里？
    调 MCP 是异步网络请求，而子图里的执行节点是同步函数。
    放在这一层，既不用把子图改成全异步，又能让所有真实外部调用集中在一处，
    以后加日志、加重试、加人工确认都只改这里。
"""
import asyncio
import re

from langchain_core.messages import AIMessage, BaseMessage

from app.agent.state.customer_state import CustomerServiceState
from app.agent.node.after_sales.graph.after_sales_subgraph import AfterSalesSubGraph
from app.tool.mcp import AlipayMCP, add_record, make_trade_no

# 与订单子图一样：模块级单例复用，避免每次请求重新 compile
_SUBGRAPH = None


def _get_subgraph() -> AfterSalesSubGraph:
    global _SUBGRAPH
    if _SUBGRAPH is None:
        _SUBGRAPH = AfterSalesSubGraph()
    return _SUBGRAPH


def _recent_history(messages, limit: int = 6) -> list:
    """取最近几轮真实消息（过滤掉非 Message 对象）。"""
    if not messages:
        return []
    history = [m for m in messages if isinstance(m, BaseMessage)]
    return history[-limit:]


def _extract_pay_url(text: str) -> str:
    """
    从支付宝返回的 Markdown 里抠出支付链接。

    支付宝返回形如：网页支付链接: [点击完成支付](https://openapi.alipay.com/...)
    """
    match = re.search(r"\((https?://[^)]+)\)", text or "")
    return match.group(1) if match else ""


async def _run_alipay(payload: dict) -> str:
    """
    调用支付宝 MCP 完成真实动作，返回给用户看的补充说明。

    只做两件事：
        - 退款给用户（退货场景）
        - 向用户收运费（用户需承担运费时）
    """
    tool = payload.get("tool", "")
    order_id = str(payload.get("order_id") or "")
    lines: list[str] = []

    if not order_id:
        return ""

    try:
        async with AlipayMCP() as ap:
            # ---------- 退款 ----------
            if tool == "refund":
                amount = float(payload.get("amount") or 0.0)
                if amount > 0:
                    trade_no = make_trade_no("RFD", order_id)
                    result = await ap.refund(trade_no, amount, order_id, "售后退款")
                    add_record(trade_no, order_id, "REFUND", amount, result)
                    lines.append(f"已发起退款 {amount:.2f} 元，1-3 个工作日原路退回。")

            # ---------- 收运费 ----------
            freight = float(payload.get("freight_fee") or 0.0)
            if freight > 0:
                trade_no = make_trade_no("FRT", order_id)
                result = await ap.create_payment(order_id, freight, "退货运费")
                add_record(trade_no, order_id, "PAY", freight, result)
                pay_url = _extract_pay_url(result)
                if pay_url:
                    lines.append(f"需补付运费 {freight:.2f} 元，请点击支付：{pay_url}")
                else:
                    lines.append(f"需补付运费 {freight:.2f} 元。\n{result}")

    except Exception as e:
        print(f"[after_sales] 支付宝调用失败：{e}")
        lines.append("支付服务暂时不可用，已为您记录，稍后会有专员跟进。")

    return "\n\n".join(lines)


async def after_sales_node(state: CustomerServiceState) -> dict:
    """售后决策节点（wrapper）。"""
    user_input = state["messages"][-1].content
    user_id = state.get("user_id", "")
    last_order_id = str(state.get("current_order_id") or "")
    order_context = str(state.get("order_context") or "")

    # ① 调子图（异步）
    result = await _get_subgraph().agent.ainvoke({
        "user_input": user_input,
        "user_id": str(user_id),
        "chat_history": _recent_history(state.get("messages")),
        "last_order_id": last_order_id,
        "order_context": order_context,
        "clarify_count": 0,     # 本次调用内反问次数（上限 MAX_CLARIFY）
    })

    # 给用户的最终答复
    solution = result.get("solution") or "抱歉，这次售后我暂时无法处理，请稍后再试或联系人工客服。"
    # 最终决策
    decision = result.get("decision") or {}
    # 二级售后意图，见 schema.INTENT_LABELS
    intent = result.get("intent", "")
    # 订单事实
    order_facts = result.get("order_facts") or {}
    # MCP
    execute_payload = result.get("execute_payload") or {}

    # ② 执行真实动作（只有需要时才调支付宝）
    if result.get("need_execute") and execute_payload.get("tool") in ("refund", "payment"):
        extra = await _run_alipay(execute_payload)
        if extra:
            solution = f"{solution}\n\n{extra}"

    # ③ 回写实体，供下一轮追问使用
    # 从对话抽取的事实（用户口述）
    resolved_id = str((result.get("facts") or {}).get("order_id") or "")
    # 当前订单ID（用户口述或从主图取）
    current_order_id = resolved_id or last_order_id
    # 历史订单ID（主图）
    history_ids = list(state.get("current_order_ids") or [])
    # 如果当前ID不在历史订单ID，则追加进去（上限五个）
    if current_order_id and current_order_id not in history_ids:
        history_ids.append(current_order_id)
    history_ids = history_ids[-5:]

    return {
        "messages": [AIMessage(content=solution)],
        "current_step": "done",
        "current_order_id": current_order_id,
        "current_order_ids": history_ids,
        "order_context": (
            f"intent={intent}; "
            f"order_id={current_order_id}; "
            f"action={decision.get('action', '')}; "
            f"is_signed={order_facts.get('is_signed')}; "
            f"solution={solution}"
        ),
        "after_sales_result": {
            "intent": intent,
            "intent_confidence": result.get("intent_confidence"),
            "decision": decision,
            "policy_basis": result.get("policy_basis") or [],
            "conflict_type": result.get("conflict_type", ""),
            "need_human": result.get("need_human", False),
            "execute_result": result.get("execute_result") or {},
        },
    }


if __name__ == "__main__":
    from langchain_core.messages import HumanMessage

    fake_state = {
        "messages": [HumanMessage(content="订单2我要退货，尺码不合适")],
        "user_id": "1",
        "current_order_id": "2",
    }
    rs = asyncio.run(after_sales_node(fake_state))
    print("=" * 60)
    print("【答复】", rs["messages"][-1].content)
    print("=" * 60)
