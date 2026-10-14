"""
售后决策节点 - Wrapper

业务逻辑在子图（app/agent/node/after_sales/），这里只做三件事：
  1. 把 CustomerServiceState 转换成子图 AfterSalesState（含跨轮上下文）
  2. 把子图结果转换回 CustomerServiceState 的字段
  3. 把本次涉及的订单号回写到主图 current_order_id
"""
from langchain_core.messages import AIMessage

from app.agent.state.customer_state import CustomerServiceState
from app.agent.node.after_sales.graph.after_sales_subgraph import AfterSalesSubGraph

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
    history = [m for m in messages if hasattr(m, "content") and hasattr(m, "type")]
    return history[-limit:]


def after_sales_node(state: CustomerServiceState) -> dict:
    """
    售后决策节点（wrapper）。

    上下文来源：
        - state["messages"]         → 最近几轮对话
        - state["current_order_id"] → 上一轮订单号（Checkpointer 跨轮保留）
        - state["order_context"]    → 上一轮在聊什么
    """
    user_input = state["messages"][-1].content
    user_id = state.get("user_id", "")
    last_order_id = str(state.get("current_order_id") or "")
    order_context = str(state.get("order_context") or "")

    result = _get_subgraph().invoke({
        "user_input": user_input,
        "user_id": str(user_id),
        "chat_history": _recent_history(state.get("messages")),
        "last_order_id": last_order_id,
        "order_context": order_context,
        "clarify_count": 0,   # 每次新的用户输入重置反问计数
    })

    solution = result.get("solution") or "抱歉，这次售后我暂时无法处理，请稍后再试或联系人工客服。"
    decision = result.get("decision") or {}
    intent = result.get("intent", "")
    order_facts = result.get("order_facts") or {}

    # ---- 回写实体：供下一轮追问使用 ----
    resolved_id = str((result.get("facts") or {}).get("order_id") or "")
    current_order_id = resolved_id or last_order_id

    history_ids = list(state.get("current_order_ids") or [])
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
            f"days_since_receipt={order_facts.get('days_since_receipt', -1)}; "
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
        "messages": [HumanMessage(content="1号订单我要退货，已经拆封了但是有质量问题")],
        "user_id": "1",
        "current_order_id": "1",
    }
    rs = after_sales_node(fake_state)
    print("=" * 60)
    print("【答复】", rs["messages"][-1].content)
    print("【current_order_id】", rs.get("current_order_id"))
    print("【after_sales_result】", rs.get("after_sales_result"))
    print("=" * 60)
