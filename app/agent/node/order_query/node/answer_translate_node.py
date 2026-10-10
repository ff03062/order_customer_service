"""
节点 5: answer_translate_node
职责：把数据库结果翻译成中文；失败时给出友好兜底。
输入：db_result, db_error, retry_count, user_input
输出：answer
"""
from app.agent.model.my_model import MyModel
from app.agent.node.order_query.schema import STATUS_MAP
from app.agent.node.order_query.node.sql_check_node import MAX_RETRIES


_ANSWER_PROMPT = """\
你是电商客服助手。把数据库查询结果翻译成给用户看的友好中文回答。

【状态码映射表（必须严格翻译，禁止凭印象猜）】
{status_map}

【用户问题】
{user_input}

【数据库查询结果（原始 JSON，状态字段是数字）】
{db_result}

【回答要求】
1. 必须根据状态码映射表翻译数字，禁止出现「（状态码 4）」这种写法
2. 用简洁友好的中文回答，金额保留两位小数
3. 查询结果为空时：「抱歉，没有找到符合条件的记录，请确认订单号或时间范围是否正确。」
4. 只输出给用户看的回答，不要输出 SQL，不要输出 markdown 代码块

【回答】"""


def answer_translate_node(state: dict) -> dict:
    db_result = state.get("db_result", "")
    db_error = state.get("db_error", "")
    retry_count = state.get("retry_count", 0)
    user_input = state.get("user_input", "")

    if db_error and retry_count >= MAX_RETRIES:
        return {"answer": "抱歉，查询暂时无法完成，请稍后再试或联系人工客服。"}
    if db_error:
        return {"answer": "抱歉，查询失败，请稍后再试。"}

    prompt = _ANSWER_PROMPT.format(
        user_input=user_input,
        db_result=db_result or "[]",
        status_map=STATUS_MAP,
    )
    model = MyModel.get_model()
    answer = model.invoke(prompt).content.strip()

    return {"answer": answer,
            "retry_count": 0,
            }
