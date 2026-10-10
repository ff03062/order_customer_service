"""
订单查询子图（Phase 2）

结构：
  order_query/
  ├── state/    OrderQueryState
  ├── schema/   INTENT_OPTIONS, STATUS_MAP, INTENT_TO_SCHEMA, render_schema_for_intent
  ├── node/     intent_classify / schema_select / sql_generate / sql_execute / answer_translate
  └── graph/    OrderQuerySubGraph 类（draw / invoke / chat_stream）

入口：
    from app.agent.node.order_query.graph.order_subgraph import OrderQuerySubGraph
    subgraph = OrderQuerySubGraph()
    subgraph.draw()          # 画拓扑图
    result = subgraph.invoke({"user_input": "...", "user_id": "1"})
"""

from app.agent.node.order_query.graph.order_subgraph import OrderQuerySubGraph

__all__ = ["OrderQuerySubGraph"]
