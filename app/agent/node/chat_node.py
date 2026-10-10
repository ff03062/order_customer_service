"""
闲聊节点
本地模型流式输出，失败降级云端模型流式输出
"""
from langchain_core.messages import AIMessage, HumanMessage
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langgraph.config import get_stream_writer
from app.agent.model.my_model import MyModel
from app.agent.state.customer_state import CustomerServiceState
from app.prompt.bulider_prompt import BuilderPromptYaml

CHAT_PROMPT = BuilderPromptYaml.get_prompt("chat_node.yaml")

# ===== 预建 agent（仅流式，不保留非流式兜底）=====

agent_local = None
try:
    agent_local = create_agent(
        model=MyModel.get_local_model(),
        system_prompt=CHAT_PROMPT,
        middleware=[
            ModelCallLimitMiddleware(thread_limit=5, exit_behavior="end")
        ],
    )
except Exception as e:
    print(f"[chat_node] 创建本地模型 agent 失败: {e}")

agent_cloud = None
try:
    agent_cloud = create_agent(
        model=MyModel.get_model(),
        system_prompt=CHAT_PROMPT,
        middleware=[
            ModelCallLimitMiddleware(thread_limit=5, exit_behavior="end")
        ],
    )
except Exception as e2:
    print(f"[chat_node] 创建云端模型 agent 失败: {e2}")


async def _stream_agent(agent, user_input: dict) -> str:
    """对指定 agent 执行流式调用，返回完整文本。"""
    writer = get_stream_writer()
    result_parts: list[str] = []

    async for chunk in agent.astream(user_input, stream_mode="messages"):
        msg = chunk[0] if isinstance(chunk, tuple) else chunk
        content = getattr(msg, "content", None)
        if not content:
            continue
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        result_parts.append(content)
        # writer(content)

    return "".join(result_parts)


async def chat_node_stream(state: CustomerServiceState) -> dict:
    """
    闲聊节点（纯流式）。
    本地模型优先，流式输出；失败降级云端模型流式输出。
    get_stream_writer() 将 chunk 实时推到 SSE，节点返回值仅用于图状态通知。
    """
    user_input = {"messages": state["messages"]}
    print(f"[chat_node] 输入: {state['messages'][-1].content[:50]}...")

    try:
        if agent_local is not None:
            result = await _stream_agent(agent_local, user_input)
            return {"messages": [AIMessage(content=result)], "current_step": "done"}
    except Exception as e1:
        print(f"[chat_node] 本地模型流式失败: {e1}，切换云端...")

    try:
        if agent_cloud is not None:
            result = await _stream_agent(agent_cloud, user_input)
            return {"messages": [AIMessage(content=result)], "current_step": "done"}
    except Exception as e2:
        print(f"[chat_node] 云端模型也失败: {e2}")

    return {
        "messages": [AIMessage(content="抱歉，服务暂时不可用，请稍后再试。")],
        "current_step": "done",
    }
