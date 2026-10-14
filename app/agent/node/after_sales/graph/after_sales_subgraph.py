"""
售后决策子图。

调用方式：
    from app.agent.node.after_sales.graph.after_sales_subgraph import AfterSalesSubGraph

    subgraph = AfterSalesSubGraph()
    result = subgraph.invoke({
        "user_input": "这单我要退货，已经拆封了但有质量问题",
        "user_id": "1",
        "chat_history": [...],            # 可选
        "last_order_id": "202609280001",  # 可选，来自主图 current_order_id
    })
    print(result["solution"])

拓扑：
    START
      ↓
    intent_slot_node          ① 二级意图 + 槽位抽取（LLM + 结构化输出）
      │
      ├─ 缺必需槽位/低置信度 → clarify_node → END（等用户补充）
      ↓
    order_facts_node          ② 事实聚合（参数化查询，0 次 LLM）
      │
      ├─ 订单号缺失/订单不存在 → clarify_node → END
      ↓
    policy_rules_node         ③ 政策检索 + 规则判定
      │
      ├─ 命中冲突 → llm_reasoning_node ④ 灰区推理（规则↔LLM 无回边，不会死循环）
      │                    │
      │                    ├─ 需补充信息 → clarify_node → END
      │                    ↓
      └────────────────────┴→ solution_generate_node ⑤ 生成答复 + 锁定执行动作
                                 │
                                 ├─ 需执行 → action_execute_node ⑥ MCP 桩 → END
                                 └─ 仅告知 → END
"""
from langgraph.graph import StateGraph, START, END

from app.agent.node.after_sales.state import AfterSalesState
from app.agent.node.after_sales.node import (
    intent_slot_node,
    order_facts_node,
    policy_rules_node,
    llm_reasoning_node,
    solution_generate_node,
    clarify_node,
    action_execute_node,
)
from app.agent.node.after_sales.node.clarify_node import MAX_CLARIFY
from app.agent.node.after_sales.node.order_facts_node import has_order_facts


# ============================================================
# 条件边
# ============================================================

def _need_clarify(state: dict) -> bool:
    """是否还有反问额度。"""
    return int(state.get("clarify_count") or 0) < MAX_CLARIFY


def _route_after_intent_slot(state: dict) -> str:
    if state.get("need_clarify") and _need_clarify(state):
        return "clarify_node"
    return "order_facts_node"


def _route_after_facts(state: dict) -> str:
    if not has_order_facts(state):
        # 订单号已经有了但事实仍取不到 → 是数据库/数据问题，问用户也没用，转人工兜底
        if (state.get("facts") or {}).get("order_id"):
            return "solution_generate_node"
        # 订单号缺失：还有额度就问用户，没有就转人工兜底
        if _need_clarify(state):
            return "clarify_node"
        return "solution_generate_node"
    return "policy_rules_node"


def _route_after_rules(state: dict) -> str:
    if state.get("conflict_detected"):
        return "llm_reasoning_node"
    return "solution_generate_node"


def _route_after_reasoning(state: dict) -> str:
    if state.get("need_more_info") and _need_clarify(state):
        # 只对「真正还缺、且用户能提供」的信息反问。
        # LLM 有时会把已经拿到的字段（如 order_id）列进 missing_info，
        # 若不过滤就会出现"明明有订单号却还在问是哪一笔订单"的怪现象。
        facts = state.get("facts") or {}
        answerable = [s for s in (state.get("missing_slots") or []) if not facts.get(s)]
        if answerable:
            return "clarify_node"
    return "solution_generate_node"


def _route_after_solution(state: dict) -> str:
    if state.get("need_execute"):
        return "action_execute_node"
    return END


# ============================================================
# 子图定义
# ============================================================

class AfterSalesSubGraph:
    """售后决策子图（规则引擎 + LLM 混合决策）。"""

    def __init__(self, checkpoint=None):
        self.memory = checkpoint
        self.agent = self.get_agent()

    def get_agent(self):
        graph = StateGraph(AfterSalesState)

        graph.add_node("intent_slot_node", intent_slot_node)
        graph.add_node("order_facts_node", order_facts_node)
        graph.add_node("policy_rules_node", policy_rules_node)
        graph.add_node("llm_reasoning_node", llm_reasoning_node)
        graph.add_node("solution_generate_node", solution_generate_node)
        graph.add_node("clarify_node", clarify_node)
        graph.add_node("action_execute_node", action_execute_node)

        graph.add_edge(START, "intent_slot_node")

        graph.add_conditional_edges(
            "intent_slot_node",
            _route_after_intent_slot,
            {"clarify_node": "clarify_node", "order_facts_node": "order_facts_node"},
        )

        graph.add_conditional_edges(
            "order_facts_node",
            _route_after_facts,
            {
                "clarify_node": "clarify_node",
                "policy_rules_node": "policy_rules_node",
                "solution_generate_node": "solution_generate_node",
            },
        )

        graph.add_conditional_edges(
            "policy_rules_node",
            _route_after_rules,
            {
                "llm_reasoning_node": "llm_reasoning_node",
                "solution_generate_node": "solution_generate_node",
            },
        )

        graph.add_conditional_edges(
            "llm_reasoning_node",
            _route_after_reasoning,
            {"clarify_node": "clarify_node", "solution_generate_node": "solution_generate_node"},
        )

        graph.add_conditional_edges(
            "solution_generate_node",
            _route_after_solution,
            # 必须显式列出 END：路由函数返回 END 时，langgraph 会拿它当 key 查这张表，
            # 表里没有就会报 KeyError: '__end__'
            {"action_execute_node": "action_execute_node", END: END},
        )

        # 反问与执行都是终态（反问等用户下一轮补充，执行完成即结束）
        graph.add_edge("clarify_node", END)
        graph.add_edge("action_execute_node", END)

        self.agent = graph.compile()
        return self.agent

    # ---------- 对外调用 ----------

    def invoke(self, inputs: dict) -> dict:
        return self.agent.invoke(inputs)

    async def ainvoke(self, inputs: dict) -> dict:
        return await self.agent.ainvoke(inputs)

    def draw(self, filename: str = "after_sales_subgraph.png"):
        data = self.agent.get_graph().draw_mermaid_png()
        with open(filename, "wb") as f:
            f.write(data)
        print(f"已保存 {filename}")


if __name__ == "__main__":
    AfterSalesSubGraph().draw()
