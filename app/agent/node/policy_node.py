"""
售后政策节点 - RAG（工具方式传给 agent）
"""
from langchain_core.messages import AIMessage, HumanMessage
from langchain.tools import tool
from langchain.agents import create_agent
from app.agent.model.my_model import MyModel
from app.agent.state.customer_state import CustomerServiceState
from app.tool.policy_retrieval_tool import search
from app.prompt.bulider_prompt import BuilderPromptYaml
from dotenv import load_dotenv

load_dotenv()

POLICY_PROMPT = BuilderPromptYaml.get_prompt("policy_node.yaml")


@tool
def search_policies(query: str) -> str:
    """检索售后政策知识库。输入用户问题，返回相关政策片段的拼接文本。"""
    results = search(query)
    if not results:
        return ""
    return "\n".join(doc for doc, _ in results)


# ===== 预建 agent（云端优先，降级本地）=====
agent_cloud = None
try:
    agent_cloud = create_agent(
        model=MyModel.get_model(),
        system_prompt=POLICY_PROMPT,
        tools=[search_policies],
    )
except Exception as e:
    print(f"创建云端模型 agent 失败: {e}")

agent_local = None
try:
    agent_local = create_agent(
        model=MyModel.get_local_model(),
        system_prompt=POLICY_PROMPT,
        tools=[search_policies],
    )
except Exception as e2:
    print(f"创建本地模型 agent 失败: {e2}")


def policy_node(state: CustomerServiceState) -> dict:
    """售后政策节点：调用 agent 检索 + 生成回答。"""
    user_input = state["messages"][-1].content

    try:
        # 正常链路：云端
        result = agent_cloud.invoke({"messages": [HumanMessage(content=user_input)]})
        return {
            "messages": [AIMessage(content=result["messages"][-1].content)],
            "retrieved_policies": [],
            "current_step": "done",
        }

    except Exception as e:
        print(f"云端模型失败: {e}，切换本地模型...")

        try:
            # 降级链路：本地
            result = agent_local.invoke({"messages": [HumanMessage(content=user_input)]})
            return {
                "messages": [AIMessage(content=result["messages"][-1].content)],
                "retrieved_policies": [],
                "current_step": "done",
            }

        except Exception as e2:
            print(f"本地模型也失败: {e2}")
            return {
                "messages": [AIMessage(content=f"抱歉，服务暂时不可用，请稍后再试。{e2}")],
                "current_step": "done",
            }
