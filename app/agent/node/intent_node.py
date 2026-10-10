"""
意图识别节点
云端模型优先，失败自动切换本地模型
"""
from langchain_core.messages import AIMessage, HumanMessage
from langchain.agents import create_agent
from app.agent.model.my_model import MyModel
from app.agent.schema.intent_schema import IntentSchema
from app.agent.state.customer_state import CustomerServiceState
from app.prompt.bulider_prompt import BuilderPromptYaml

# 提示词模板
INTENT_PROMPT = BuilderPromptYaml.get_prompt("intent_node.yaml")

# ===== 预建 agent =====

# 正常链路：云端模型
agent_cloud = None
try:
    agent_cloud = create_agent(
        model=MyModel.get_model(),
        system_prompt=INTENT_PROMPT,
        response_format=IntentSchema
    )
except Exception as e:
    print(f"创建云端模型 agent 失败: {e}")

# 降级链路：本地模型
agent_local = None
try:
    agent_local = create_agent(
        model=MyModel.get_local_model(),
        system_prompt=INTENT_PROMPT,
        response_format=IntentSchema
    )
except Exception as e2:
    print(f"创建本地模型 agent 失败: {e2}")


def intent_node(state: CustomerServiceState) -> dict:
    """
    意图识别节点

    流程：
    1. 获取用户输入与上下文
    2. 调用云端模型识别意图
    3. 返回意图
    """
    # 获取用户输入
    last_msg = state["messages"][-1]
    user_input = ""
    if isinstance(last_msg, HumanMessage):
        user_input = last_msg.content
        print("意图识别：用户输入问题：", user_input)

    try:
        # 调用模型（优先云端）
        result = agent_cloud.invoke({
            "messages": [HumanMessage(content=user_input)]
        })

        # 解析结果
        data = result["structured_response"].model_dump()

        return {
            "intent": data.get("intent", "unknown"),
            "detail": data.get("detail", ""),
            "current_step": "intent"
        }

    except Exception as e3:
        print(f"云端模型失败: {e3}，切换本地模型...")

        # 降级到本地模型
        try:
            result = agent_local.invoke({
                "messages": [HumanMessage(content=user_input)]
            })

            data = result["structured_response"].model_dump()

            return {
                "intent": data.get("intent", "unknown"),
                "detail": data.get("detail", ""),
                "current_step": "intent"
            }

        except Exception as e4:
            print(f"本地模型也失败: {e4}")

            # 兜底：返回未知意图
            return {
                "intent": "unknown",
                "detail": f"模型异常: {e4}",
                "current_step": "intent"
            }


if __name__ == "__main__":
    state = {"messages": [HumanMessage(content="我想退货")]}
    result = intent_node(state)
    print("\n===== 最终结果 =====")
    print("intent:", result["intent"])
    print("detail:", result["detail"])
    print("current_step:", result["current_step"])