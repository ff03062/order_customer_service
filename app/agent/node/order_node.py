"""
订单查询节点 - Wrapper（Phase 2）

业务逻辑已迁移在子图
只做两件事：
  1. 把 CustomerServiceState 转换成子图 OrderQueryState
  2. 把子图结果转换回 CustomerServiceState 的字段
"""
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.state.customer_state import CustomerServiceState
from app.agent.node.order_query.graph.order_subgraph import OrderQuerySubGraph


def order_node(state: CustomerServiceState) -> dict:
    """
    订单查询节点（Phase 2 wrapper）。
    """
    user_input = state["messages"][-1].content
    user_id = state.get("user_id", "")

    subgraph = OrderQuerySubGraph()
    result = subgraph.invoke({
        "user_input": user_input,
        "user_id": str(user_id),
    })

    answer = result.get("answer", "抱歉，查询失败，请稍后再试。")
    sql = result.get("sql", "")
    sql_attempts = result.get("sql_attempts", [])
    db_result = result.get("db_result", "")
    db_error = result.get("db_error", "")
    intent = result.get("intent", "")

    return {
        "messages": [AIMessage(content=answer)],
        "current_step": "done",
        "sql": sql,
        "sql_attempts": sql_attempts,
        "db_result": db_result,
        "db_error": db_error,
        "order_context": (
            f"intent={intent}; "
            f"sql_attempts={len(sql_attempts)}; "
            f"{'失败：' + db_error if db_error else '成功'}；"
            f"answer={answer}"
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
    print("【order_context】", result.get("order_context"))
    print("【sql】", result.get("sql"))
    print("【sql_attempts】", result.get("sql_attempts"))
    print("【db_result】", result.get("db_result"))
    print("【db_error】", result.get("db_error"))
    print("=" * 60)
