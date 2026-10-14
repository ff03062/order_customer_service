"""
节点 2: order_facts_node
职责：事实聚合 —— 按固定字段集合查询订单事实，并用库里已有的售后单补全槽位。

为什么不用订单查询子图？
    订单子图解决「不知道用户要查什么」，需要 Text2SQL 的灵活性；
    售后需要的字段集合是**固定的**（时效、金额、品类、运费险、已有售后单），
    用参数化查询直接取，0 次 LLM 调用、结果精确、失败面小。

为什么要在这一步补槽位？
    after_sales 表里已经有 type（1质量问题/2退货/3换货/4维修）和
    description（文字描述）。订单号 + user_id 一关联就能确认售后原因，
    此时再去问用户"请问是什么原因"就是多余交互。
    数据里能拿到的信息，绝不该转成对用户的追问。

输入：facts["order_id"] / user_id / intent
输出：order_facts / db_error / facts（补全后）/ missing_slots / need_clarify
"""
from app.tool.order_facts_tool import get_order_facts
from app.agent.node.after_sales.schema import apply_existing_after_sales


def order_facts_node(state: dict) -> dict:
    facts = dict(state.get("facts") or {})
    intent = state.get("intent", "")
    order_id = str(facts.get("order_id") or "").strip()
    user_id = state.get("user_id", "")

    if not order_id:
        print("[order_facts] 缺少订单号，跳过事实聚合")
        return {"order_facts": {}, "db_error": "missing_order_id"}

    order_facts = get_order_facts(order_id, user_id)

    if not order_facts.get("found"):
        # 订单不存在/不属于该用户 —— 不抛异常，交给上层按「事实缺失」处理
        print(f"[order_facts] 未取到订单事实：{order_facts.get('error')}")
        return {
            "order_facts": order_facts,
            "db_error": order_facts.get("error") or "订单查询失败",
        }

    existing = order_facts.get("existing_after_sales") or {}

    # 用已有售后单补全「原因」等槽位，避免无意义追问
    missing = apply_existing_after_sales(intent, facts, order_facts)

    print(
        f"[order_facts] order_id={order_id} status={order_facts['order_status']} "
        f"is_signed={order_facts['is_signed']}({order_facts['signed_source']}) "
        f"days_since_receipt={order_facts['days_since_receipt']} "
        f"days_since_purchase={order_facts['days_since_purchase']} "
        f"categories={order_facts['categories']} "
        f"insurance={order_facts['has_freight_insurance']} "
        f"existing_after_sales={existing.get('type_text') or existing.get('type') or '无'}"
        f"{'/' + existing.get('description') if existing.get('description') else ''} "
        f"missing={missing}"
    )

    return {
        "order_facts": order_facts,
        "db_error": "",
        "facts": facts,
        "missing_slots": missing,
        # 库里的信息补全后若不再缺槽位，就撤回反问意图
        "need_clarify": bool(missing),
    }


def has_order_facts(state: dict) -> bool:
    """供条件边使用：事实是否成功取到。"""
    return bool((state.get("order_facts") or {}).get("found"))
