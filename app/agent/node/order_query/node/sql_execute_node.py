"""
节点 4: sql_execute_node
职责：执行 SQL，返回结果，并回收本次命中的订单号。

输入：sql, user_id, user_input
输出：db_result / db_error / resolved_order_ids

流程：
  - 如果有 error → return db_error，打回 sql_generate_node 重试
  - 如果成功 → return db_result + resolved_order_ids，去 answer_translate_node

为什么要把 order_id 提出来：
    主图 CustomerServiceState 需要 current_order_id 来支撑下一轮
    「那它到哪了」这类省略指代的追问。之前该字段定义了却从未被写入，
    是整个多轮追问链路断掉的根本原因。
"""
import json
import re

from app.tool.mysql_tool import mysql_tool

# 订单号可能是纯数字（BIGINT），也可能是带前缀的业务单号
ORDER_ID_PATTERN = re.compile(r"^\d{6,}$")


def _extract_order_ids(rows: list) -> list[str]:
    """
    从查询结果中回收订单号（去重、保持出现顺序）。

    只接受两类值：
        - 长度 >= 6 的纯数字（order_id 为 BIGINT 时的形态）
        - 形如 ORD/订单 前缀的业务单号
    """
    if not rows:
        return []

    found: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if value is None:
                continue
            key_lower = str(key).lower()
            if key_lower not in ("order_id", "order_sn", "order_no"):
                continue
            text = str(value)
            if text.isdigit() and len(text) >= 6 and text not in found:
                found.append(text)
    return found


def _extract_order_ids_from_input(user_input: str) -> list[str]:
    """从用户原话里兜底提取订单号（数据库返回空结果时仍能记录用户指代）。"""
    if not user_input:
        return []
    found = []
    for token in re.findall(r"\d{6,}", str(user_input)):
        if token not in found:
            found.append(token)
    return found


def sql_execute_node(state: dict) -> dict:
    """
    执行 SQL。
    - 有 error → 打回重试
    - 成功 → 继续，并回收订单号
    """
    sql = state.get("sql", "")
    user_id = state.get("user_id", "")

    print("=" * 60)
    print("[SQL 执行]")
    print(f"SQL: {sql}")
    print(f"user_id: {user_id}")
    print("=" * 60)

    try:
        result = mysql_tool.invoke({"sql": sql, "user_id": user_id})
    except Exception as e:
        print(f"[SQL 执行异常] {e}")
        result = str(e)

    # mysql_tool 返回错误时是纯文本字符串（非 JSON）
    if _is_error(result):
        print(f"[SQL 执行失败] {result}")
        return {
            "db_result": "",
            "db_error": result,
        }

    rows = json.loads(result) if result else []
    order_ids = _extract_order_ids(rows)
    if not order_ids:
        # 结果为空或未含 order_id 字段时，退回用户原话里的数字，保证追问链路不断
        order_ids = _extract_order_ids_from_input(
            state.get("rewritten_input") or state.get("user_input", "")
        )

    print(f"[SQL 执行成功] 返回 {len(rows)} 条记录，命中订单号: {order_ids}")
    return {
        "db_result": result,
        "db_error": "",
        "resolved_order_ids": order_ids,
    }


def _is_error(raw: str) -> bool:
    """mysql_tool 返回错误时是纯文本字符串（非 JSON）。"""
    if not raw:
        return True
    try:
        json.loads(raw)
        return False
    except (json.JSONDecodeError, ValueError):
        return True
