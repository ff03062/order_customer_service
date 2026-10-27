"""
订单查询节点 - Wrapper

业务逻辑在子图（app/agent/node/order_query/），这里只做三件事：
  1. 把 CustomerServiceState 转换成子图 OrderQueryState（含跨轮上下文）
  2. 把子图结果转换回 CustomerServiceState 的字段
  3. 把本次命中的订单号回写到主图 current_order_id（支撑下一轮追问）
"""
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.state.customer_state import CustomerServiceState
from app.agent.node.order_query.graph.order_subgraph import OrderQuerySubGraph

# 子图通过模块级单例复用：子图 compile 有开销，不应每次请求重建
_SUBGRAPH = None


def _get_subgraph() -> OrderQuerySubGraph:
    global _SUBGRAPH
    if _SUBGRAPH is None:
        _SUBGRAPH = OrderQuerySubGraph()
    return _SUBGRAPH


def _recent_history(messages, limit: int = 6) -> list:
    """取最近几轮真实消息（过滤掉非 Message 对象，如 system 提示词 dict）。"""
    if not messages:
        return []
    history = [m for m in messages if hasattr(m, "content") and hasattr(m, "type")]
    return history[-limit:]


def order_node(state: CustomerServiceState) -> dict:
    """
    订单查询节点（wrapper）。

    上下文来源（跨轮追问的关键）：
        - state["messages"]            → 最近几轮对话
        - state["current_order_id"]    → 上一轮解析出的订单号（Checkpointer 跨轮保留）
        - state["order_context"]       → 上一轮在聊什么
    """
    user_input = state["messages"][-1].content
    user_id = state.get("user_id", "")

    # 跨轮上下文：来自主图 state（由 Checkpointer 从上一轮恢复）
    last_order_id = str(state.get("current_order_id") or "")  # 当前对话ID
    order_context = str(state.get("order_context") or "")   # 上下文描述，如"用户在查第3个订单，金额299元"

    result = _get_subgraph().agent.invoke({
        "user_input": user_input,
        "user_id": str(user_id),
        # ---- 上下文注入，解决「那它到哪了」这类省略指代 ----
        "chat_history": _recent_history(state.get("messages")),
        "last_order_id": last_order_id,
        "order_context": order_context,
    })

    answer = result.get("answer", "订单子图查询失败，无answer，请稍后重试")
    sql = result.get("sql", "")                     # 解析后的 SQL
    sql_attempts = result.get("sql_attempts", [])   # 每次尝试的原始输出
    db_result = result.get("db_result", "")         # 数据库返回的 JSON 字符串（成功时）
    db_error = result.get("db_error", "")           # 检查/执行失败信息（成功时为空）
    intent = result.get("intent", "")               # "query_orders" | "query_order_items" | "query_logistics" | "query_after_sales" | "query_products" | "chitchat"
    rewritten_input = result.get("rewritten_input", user_input)     # query_rewrite_node 改写后的自包含问题

    # ---- 回写实体：供下一轮追问使用 ----
    resolved = result.get("resolved_order_ids") or []       # 本次查询实际命中的订单号（供主图回写）
    current_order_id = resolved[0] if resolved else last_order_id
    # 累积出现过的订单号（去重保序），最多保留最近 5 个
    history_ids = list(state.get("current_order_ids") or [])
    for oid in resolved:
        if oid not in history_ids:
            history_ids.append(oid)
    history_ids = history_ids[-5:]

    return {
        "messages": [AIMessage(content=answer)],
        "current_step": "done",
        "sql": sql,
        "sql_attempts": sql_attempts,
        "db_result": db_result,
        "db_error": db_error,
        "current_order_id": current_order_id,
        "current_order_ids": history_ids,
        "order_context": (
            f"意图 (intent)={intent}; "
            f"自包含问题 (rewritten)={rewritten_input}; "
            f"订单id (order_id)={current_order_id}; "
            f"每次尝试原始输出 (sql_attempts)={len(sql_attempts)}; "
            f"{'失败：' + db_error if db_error else '成功'}；"
            f"给用户的中文回答 (answer) ={answer}"
        ),
    }


if __name__ == "__main__":
    fake_state = {
        "messages": [HumanMessage(content="我的1号订单物流情况")],
        "user_id": "1",
    }

    result = order_node(fake_state)

    print("=" * 60)
    print("【返回的 messages】")
    print(result["messages"][-1].content)
    print("=" * 60)
    print("【current_step】", result.get("current_step"))
    print("【current_order_id】", result.get("current_order_id"))
    print("【current_order_ids】", result.get("current_order_ids"))
    print("【order_context】", result.get("order_context"))
    print("【sql】", result.get("sql"))
    print("【sql_attempts】", result.get("sql_attempts"))
    print("【db_result】", result.get("db_result"))
    print("【db_error】", result.get("db_error"))
    print("=" * 60)
