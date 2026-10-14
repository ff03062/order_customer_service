"""
节点 0: query_rewrite_node
职责：把「带省略指代」的用户问题改写成自包含的完整问题，供后续节点使用。

为什么需要它：
    用户在第 2 轮问「那它到哪了」，如果不改写：
      - intent_classify_node 拿到的是残缺问题
      - sql_generate_node 无法确定订单号，生成的 SQL 必然查不到数据
    改写成「订单 202609280001 的物流到哪了」之后，
    后续所有节点拿到的都是自包含问题，提示词一个字都不用改。

输入：user_input / chat_history / last_order_id / order_context
输出：rewritten_input

节省开销的设计：
    只有在检测到指代词、且存在可用上下文时才调用 LLM；
    否则直接原样透传（rewritten_input = user_input），避免每轮多付一次模型调用。
"""
import re

from langchain_core.messages import HumanMessage

from app.agent.model.my_model import MyModel

# 指代词 / 省略主语的信号
PRONOUN_PATTERN = re.compile(
    r"它|他|她|它们|这个|那个|这些|那些|这单|这笔|这件|这台|这货"
    r"|那一单|那笔|那件|那台|刚才|上面说|前面说|之前说|上次说|刚说|刚提"
    r"|此单|该单|该笔"
)

REWRITE_PROMPT = """你是电商客服系统的「问题改写助手」。

【最近对话】
{history}

【上一轮已知实体】
最近订单号：{last_order_id}
上一轮话题：{order_context}

【用户当前问题】
{user_input}

【任务】
把用户当前问题改写成一个「不依赖上下文、可独立理解」的完整问题。

【规则】
1. 问题中出现"它/那个/这笔/上面说的"等指代时，用【上一轮已知实体】或【最近对话】中的具体信息替换
2. 当前问题是全新话题（出现新的订单号、新的诉求）时，原样返回，不要强行塞入旧实体
3. 不要补充用户没有提供的信息，不要改变用户的诉求
4. 禁止输出"改写为：""改写后："等任何前缀或解释
5. 只输出改写后的问题本身"""


def _format_history(chat_history) -> str:
    """把最近几轮消息格式化成纯文本。"""
    if not chat_history:
        return "（无）"
    lines = []
    for msg in chat_history[-6:]:
        content = getattr(msg, "content", None)
        if not content or not isinstance(content, str):
            continue
        role = "用户" if getattr(msg, "type", "") == "human" else "客服"
        lines.append(f"{role}：{content[:200]}")
    return "\n".join(lines) if lines else "（无）"


def _sanitize(rewritten: str) -> str:
    """清洗模型输出，剔除常见前缀与多余引号。"""
    text = (rewritten or "").strip()
    text = re.sub(r"^(改写后?[的问题]*[:：]|rephrased question[:：])\s*", "", text, flags=re.IGNORECASE)
    text = text.strip().strip("`").strip()
    text = text.strip('"').strip("“”").strip()
    return text


def rewrite_question(user_input: str, chat_history, last_order_id: str, order_context: str) -> str:
    """
    对外暴露的改写函数（子图节点与售后子图共用）。
    返回改写后的问题；不需要改写或改写失败时返回原问题。
    """
    user_input = (user_input or "").strip()
    if not user_input:
        return ""

    has_context = bool(last_order_id or order_context or chat_history)
    if not (PRONOUN_PATTERN.search(user_input) and has_context):
        return user_input   # 无指代 → 不改写，省一次模型调用

    prompt = REWRITE_PROMPT.format(
        history=_format_history(chat_history),
        last_order_id=last_order_id or "（无）",
        order_context=(order_context or "（无）")[:300],
        user_input=user_input,
    )

    for model_getter in ("get_model", "get_local_model"):
        try:
            model = getattr(MyModel, model_getter)()
            rewritten = _sanitize(model.invoke(prompt).content)
            # 长度合理性校验：过短或过长都视为改写失败，回退原问题
            if rewritten and 1 <= len(rewritten) <= max(60, len(user_input) * 4):
                print(f"[query_rewrite] {user_input}  →  {rewritten}")
                return rewritten
        except Exception as e:
            print(f"[query_rewrite] {model_getter} 失败: {e}")

    return user_input


def query_rewrite_node(state: dict) -> dict:
    """子图节点：把改写结果写入 rewritten_input。"""
    rewritten = rewrite_question(
        user_input=state.get("user_input", ""),
        chat_history=state.get("chat_history") or [],
        last_order_id=state.get("last_order_id", ""),
        order_context=state.get("order_context", ""),
    )
    return {"rewritten_input": rewritten}


if __name__ == "__main__":
    from langchain_core.messages import AIMessage

    demo_history = [
        HumanMessage(content="我的订单 202609280001 到哪了"),
        AIMessage(content="您的订单已发货，正在运输中。"),
    ]
    print(rewrite_question("那它什么时候能到", demo_history, "202609280001", "查询该订单物流"))
    print(rewrite_question("你好", demo_history, "202609280001", "查询该订单物流"))
