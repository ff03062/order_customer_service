"""
闲聊节点（兜底）

【流式在哪做】
    不在这个节点里做。外层的 `graph.astream(stream_mode="messages")` 会把本节点产生的
    token 逐条推给前端，节点只负责「返回一条 AI 消息」。

【为什么不能用 create_agent】
    实测：节点里直接 `await model.ainvoke(...)` → 外层收到 14 个 token 事件（真流式）；
          节点里套 `create_agent(...)` 再 astream → 外层只收到 1 个完整消息事件。
    原因在 langgraph/pregel/_messages.py 的 on_chat_model_start：
    没开 subgraphs 时，嵌套子图里的模型调用会被过滤掉，token 根本冒不出来。

    所以这里直接调模型。少了 create_agent 这一层，反而更简单。
"""
from langchain_core.messages import SystemMessage

from app.agent.model.my_model import MyModel
from app.agent.state.customer_state import CustomerServiceState
from app.prompt.bulider_prompt import BuilderPromptYaml

CHAT_PROMPT = BuilderPromptYaml.get_prompt("chat_node.yaml")


async def chat_node(state: CustomerServiceState) -> dict:
    """闲聊兜底：云端优先，连不上再退本地。"""
    system = CHAT_PROMPT
    memory_context = state.get("memory_context") or ""
    if memory_context:
        system = f"{system}\n\n【记忆上下文】\n{memory_context}"

    # 会话里出现过的订单号，直接给模型：窗口记忆被截断时也能答"我们聊过哪些订单"
    order_ids = state.get("current_order_ids") or []
    if order_ids:
        system = f"{system}\n\n【本次会话出现过的订单号】{', '.join(order_ids)}"

    messages = [SystemMessage(content=system), *state["messages"]]
    print(f"[chat_node] 输入: {getattr(state['messages'][-1], 'content', '')[:50]}...")

    try:
        reply = await MyModel.get_model().ainvoke(messages)
    except Exception as e:
        print(f"[chat_node] 云端失败: {e}，切换本地")
        reply = await MyModel.get_local_model().ainvoke(messages)

    return {"messages": [reply], "current_step": "done"}
