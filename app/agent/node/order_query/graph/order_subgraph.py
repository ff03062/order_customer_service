"""
订单查询子图。

调用方式：
    from app.agent.node.order_query.graph.order_subgraph import OrderQuerySubGraph

    subgraph = OrderQuerySubGraph(checkpoint)
    subgraph.draw()
    result = subgraph.invoke({"user_input": "我的订单", "user_id": "1"})
    print(result["answer"])
"""
from langgraph.graph import StateGraph, START, END

from app.agent.node.order_query.state import OrderQueryState
from app.agent.node.order_query.node import (
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
      sql_execute_node          → 执行 SQL
        ↓                       ├─ db_error → sql_generate_node（重试）
        │                       └─ 成功 → answer_translate_node
      answer_translate_node     → 状态翻译 + 兜底
        ↓
       END
    """

    def __init__(self, checkpoint=None):
        self.memory = checkpoint
        self.agent = self.get_agent()

    def get_agent(self):
        graph = StateGraph(OrderQueryState)

        graph.add_node("intent_classify_node", intent_classify_node)
        graph.add_node("schema_select_node", schema_select_node)
        graph.add_node("sql_generate_node", sql_generate_node)
        graph.add_node("sql_check_node", sql_check_node)
        graph.add_node("sql_execute_node", sql_execute_node)
        graph.add_node("answer_translate_node", answer_translate_node)

        graph.add_edge(START, "intent_classify_node")
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

        self.agent = graph.compile(checkpointer=self.memory)
        return self.agent

    def invoke(self, inputs: dict) -> dict:
        return self.agent.invoke(inputs)

    async def ainvoke(self, inputs: dict) -> dict:
        return await self.agent.ainvoke(inputs)

    async def chat_stream(self, user_input: str, user_id: str, session_id: str):
        result = await self.ainvoke({
            "user_input": user_input,
            "user_id": user_id,
        })
        yield result.get("answer", "")

    def draw(self, filename: str = "order_subgraph.png"):
        data = self.agent.get_graph().draw_mermaid_png()
        with open(filename, "wb") as f:
            f.write(data)
        print(f"已保存 {filename}")

if __name__ == '__main__':
    OrderQuerySubGraph().draw()