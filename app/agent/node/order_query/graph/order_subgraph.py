"""
订单查询子图。

调用方式：
    from app.agent.node.order_query.graph.order_subgraph import OrderQuerySubGraph

    subgraph = OrderQuerySubGraph()
    subgraph.draw()
    result = subgraph.agent.invoke({
        "user_input": "那它到哪了",              # 本轮用户原话
        "user_id": "1",
        "chat_history": [...],                  # 最近几轮消息（可选）
        "last_order_id": "202609280001",        # 上一轮订单号（可选）
        "order_context": "查询该订单物流",       # 上一轮话题（可选）
    })
    print(result["answer"])

上下文相关字段说明（由主图 order_node 注入）：
    - chat_history / last_order_id / order_context 供 query_rewrite_node 消解省略指代
    - resolved_order_ids 由 sql_execute_node 产出，主图据此回写 current_order_id
"""
from langgraph.graph import StateGraph, START, END

from app.agent.node.order_query.state import OrderQueryState
from app.agent.node.order_query.node import (
    query_rewrite_node,
    intent_classify_node,
    schema_select_node,
    sql_generate_node,
    sql_check_node,
    sql_execute_node,
    answer_translate_node,
)

MAX_RETRIES = 3
def _route_after_check(state: dict) -> str:
    result = state.get("sql_check_result", "")
    # 重试条件：JSON 解析失败 / 空 SQL / 危险 SQL / 缺少 user_id
    if result in ("invalid_json", "empty", "dangerous", "no_user_id"):
        return "sql_generate_node"
    if result == "exhausted":
        return "answer_translate_node"
    if result in ("pass", ""):
        return "sql_execute_node"
    return "answer_translate_node"


def _route_after_execute(state: dict) -> str:
    print(f"[route] retry={state.get('retry_count')}, db_error={bool(state.get('db_error'))}")
    if state.get("db_error", ""):
        if state.get("retry_count", 0) >= 3:
            return "answer_translate_node"  # 超限就放弃，去翻译/兜底
        return "sql_generate_node"
    return "answer_translate_node"


class OrderQuerySubGraph:
    """
    订单查询子图。

    拓扑：
      START
        ↓
      query_rewrite_node        → 结合 chat_history + last_order_id 消解省略指代
        ↓                         （无指代时不调用模型，直接透传）
      intent_classify_node      → 判断查询类型（订单/物流/售后...）
        ↓
      schema_select_node        → 根据意图裁剪相关表字段
        ↓
      sql_generate_node         → 生成 SQL（打印 raw_output 到控制台）
        ↓
      sql_check_node            → 检查 SQL（剥 markdown / json.loads / 防注入 / user_id）
        ↓                       ├─ invalid_json / empty / dangerous / no_user_id → sql_generate_node（重试）
        │                       ├─ exhausted → answer_translate_node
        │                       └─ pass → sql_execute_node
      sql_execute_node          → 执行 SQL，回收 resolved_order_ids
        ↓                       ├─ db_error → sql_generate_node（重试）
        │                       └─ 成功 → answer_translate_node
      answer_translate_node     → 状态翻译 + 兜底
        ↓
       END
    """

    def __init__(self):
        self.agent = self._build()

    def _build(self):
        graph = StateGraph(OrderQueryState)

        graph.add_node("query_rewrite_node", query_rewrite_node)
        graph.add_node("intent_classify_node", intent_classify_node)
        graph.add_node("schema_select_node", schema_select_node)
        graph.add_node("sql_generate_node", sql_generate_node)
        graph.add_node("sql_check_node", sql_check_node)
        graph.add_node("sql_execute_node", sql_execute_node)
        graph.add_node("answer_translate_node", answer_translate_node)

        graph.add_edge(START, "query_rewrite_node")
        graph.add_edge("query_rewrite_node", "intent_classify_node")
        graph.add_edge("intent_classify_node", "schema_select_node")
        graph.add_edge("schema_select_node", "sql_generate_node")
        graph.add_edge("sql_generate_node", "sql_check_node")

        graph.add_conditional_edges(
            "sql_check_node",
            _route_after_check,
            {
                "sql_generate_node": "sql_generate_node",
                "sql_execute_node": "sql_execute_node",
                "answer_translate_node": "answer_translate_node",
            },
        )

        graph.add_conditional_edges(
            "sql_execute_node",
            _route_after_execute,
            {
                "sql_generate_node": "sql_generate_node",
                "answer_translate_node": "answer_translate_node",
            },
        )

        graph.add_edge("answer_translate_node", END)

        return graph.compile()

    def draw(self, filename: str = "order_subgraph.png"):
        data = self.agent.get_graph().draw_mermaid_png()
        with open(filename, "wb") as f:
            f.write(data)
        print(f"已保存 {filename}")

if __name__ == '__main__':
    OrderQuerySubGraph().draw()