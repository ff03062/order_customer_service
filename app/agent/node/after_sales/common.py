"""
售后子图内部共用的三个小工具。

存在的唯一理由：这些东西原来在 intent_slot_node / llm_reasoning_node /
solution_generate_node 里各抄了一份（同一段 60 行的「结构化输出 + 文本兜底」
写了两遍，同一段订单事实格式化写了两遍）。合并到这里，改一处就够。

不放到 app/tool/ 的原因：format_order_facts 是售后专属的，不是通用工具。
"""
import json

from langchain_core.messages import HumanMessage

from app.agent.model.my_model import MyModel

# 云端模型优先，失败降级本地模型（本项目一直有这个约定）
_MODEL_GETTERS = ("get_model", "get_local_model")


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象（自动剥掉 ``` 代码块）。"""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def coerce(schema, result):
    """
    把模型返回值统一成 Pydantic 模型。

    实测本项目的云端模型即使调用了 with_structured_output，也会把结果包成
    [ {...} ]（JSON 数组）返回 —— 直接 schema(**list) 会报
    "Input should be an object [type=model_type]"。
    这里统一剥壳：list → 取首个 dict；dict → 兼容 result/output/data 再包一层。
    """
    if result is None:
        return None
    if isinstance(result, schema):
        return result

    payload = result
    if isinstance(payload, str):
        payload = extract_json(payload)
    if isinstance(payload, list):
        payload = next((item for item in payload if isinstance(item, dict)), None)
    if isinstance(payload, dict):
        for key in ("result", "output", "data"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                payload = inner
                break
        try:
            return schema(**payload)
        except Exception as e:
            print(f"[llm] 结构化结果字段校验失败: {e}")
    return None


def structured_call(schema, prompt: str):
    """
    调模型拿结构化结果。

    两级兜底：
        1. with_structured_output（约束解码，最稳）
        2. 该模型不支持时，退回「纯文本 + JSON 解析」
    每一级都按「云端 → 本地」的顺序试。
    """
    for getter in _MODEL_GETTERS:
        try:
            model = getattr(MyModel, getter)()
        except Exception as e:
            print(f"[llm] {getter} 初始化失败: {e}")
            continue

        try:
            structured = model.with_structured_output(schema, method="json_mode")
            parsed = coerce(schema, structured.invoke([HumanMessage(content=prompt)]))
            if parsed is not None:
                return parsed
        except Exception as e:
            print(f"[llm] {getter} 结构化输出失败，改用文本解析: {e}")

        try:
            text = model.invoke([HumanMessage(content=prompt)]).content
            data = extract_json(text)
            if data:
                return schema(**data)
        except Exception as e:
            print(f"[llm] {getter} 文本解析失败: {e}")

    return None


def text_call(prompt: str) -> str:
    """调模型拿一段纯文本。失败返回空串，由调用方决定兜底话术。"""
    for getter in _MODEL_GETTERS:
        try:
            model = getattr(MyModel, getter)()
            text = model.invoke([HumanMessage(content=prompt)]).content
            if text and isinstance(text, str) and text.strip():
                return text.strip()
        except Exception as e:
            print(f"[llm] {getter} 调用失败: {e}")
    return ""


def format_order_facts(order_facts: dict) -> str:
    """
    把订单事实渲染成给模型看的文本。

    灰区推理和答复生成用的是同一份事实，所以只能有一个渲染版本 ——
    分开写两份的结果就是两边说的话对不上。
    """
    if not order_facts or not order_facts.get("found"):
        return "（未取到订单事实）"

    if order_facts.get("is_signed"):
        signed = (
            f"已签收，签收距今 {order_facts.get('days_since_receipt')} 天"
            f"（依据：{order_facts.get('signed_source') or '未知'}）"
        )
    else:
        signed = (
            f"未签收（物流状态 {order_facts.get('logistics_status')}，"
            f"依据：{order_facts.get('signed_source') or '未知'}）"
            "—— 商品在途中，七天无理由时效尚未开始，用户可随时退货"
        )

    return (
        f"订单状态：{order_facts.get('order_status')}\n"
        f"签收情况：{signed}\n"
        f"下单距今：{order_facts.get('days_since_purchase')} 天\n"
        f"商品品类：{order_facts.get('categories')}\n"
        f"实付金额：{order_facts.get('pay_amount')}\n"
        f"运费险：{'有' if order_facts.get('has_freight_insurance') else '无'}\n"
        f"已有售后单：{'是' if order_facts.get('has_existing_after_sales') else '否'}"
    )
