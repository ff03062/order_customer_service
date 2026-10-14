"""
售后决策子图（After-Sales Decision Subgraph）

结构：
    after_sales/
    ├── state/    AfterSalesState
    ├── schema/   二级意图 / 槽位 / 规则表 / 冲突类型
    ├── node/     intent_slot / order_facts / policy_rules /
    │             llm_reasoning / solution_generate / clarify / action_execute
    └── graph/    AfterSalesSubGraph 类（invoke / ainvoke / draw）

入口：
    from app.agent.node.after_sales.graph.after_sales_subgraph import AfterSalesSubGraph
    subgraph = AfterSalesSubGraph()
    result = subgraph.invoke({"user_input": "我要退货", "user_id": "1"})
    print(result["solution"])
"""

from app.agent.node.after_sales.graph.after_sales_subgraph import AfterSalesSubGraph

__all__ = ["AfterSalesSubGraph"]
