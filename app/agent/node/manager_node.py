"""
管理员节点 - 中枢路由
根据意图识别结果，决定下一步走哪个节点
"""
from app.agent.state.customer_state import CustomerServiceState
from langgraph.types import Command
from langgraph.graph import END


def manager_node(state: CustomerServiceState) -> Command:
    """
    管理员节点 - 中枢路由

    根据意图识别结果，决定下一步路由：
    - order_query → order_node（订单查询子图）
    - after_sales → after_sales_node（售后决策子图）
    - chat → chat_node（闲聊）
    - unknown → chat_node（兜底闲聊）
    """
    intent = state.get("intent", "unknown")
    current_step = state.get("current_step", "start")

    print(f"当前状态意图: {intent}, 步骤: {current_step}")

    if intent == "order_query":
        return Command(goto="order")
    elif intent == "after_sales":
        return Command(goto="after_sales")
    else:
        return Command(goto="chat")
