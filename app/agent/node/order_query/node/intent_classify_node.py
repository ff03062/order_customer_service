"""
节点 1: intent_classify_node
职责：判断用户想查什么，输出枚举 intent。
输入：user_input
输出：intent
"""
from app.agent.model.my_model import MyModel
from app.agent.node.order_query.schema import INTENT_OPTIONS, STATUS_MAP


INTENT_PROMPT = """
你是客服系统的意图路由器。分析用户问题，只返回一个意图枚举值，不做任何解释。

【意图选项】
{options}

【状态码映射表（用于理解用户输入中的状态词）】
{status_map}

【用户问题】
{user_input}

【意图】
"""


def intent_classify_node(state: dict) -> dict:
    # 优先使用 query_rewrite_node 改写后的自包含问题（无指代时它与原问题相同）
    user_input = state.get("rewritten_input") or state["user_input"]
    model = MyModel.get_model()

    prompt = INTENT_PROMPT.format(
        options=" | ".join(INTENT_OPTIONS),
        user_input=user_input,
        status_map=STATUS_MAP,
    )
    raw = model.invoke(prompt).content.strip()

    # 模型可能返回多余文本，做归一化
    intent = raw.strip().strip("`'\"。，.").lower()

    # 容错：如果返回不在枚举里，回退到 query_orders（最常见）
    if intent not in INTENT_OPTIONS:
        intent = "query_orders"

    return {"intent": intent}
