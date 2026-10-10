"""
节点 4: sql_execute_node
职责：执行 SQL，返回结果。

输入：sql, user_id
输出：db_result / db_error

流程：
  - 如果有 error → return db_error，打回 sql_generate_node 重试
  - 如果成功 → return db_result，去 answer_translate_node
"""
import json

from app.tool.mysql_tool import mysql_tool


def sql_execute_node(state: dict) -> dict:
    """
    执行 SQL。
    - 有 error → 打回重试
    - 成功 → 继续
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
            "retry_count": state.get("retry_count", 0) + 1,
        }

    print(f"[SQL 执行成功] 返回 {len(json.loads(result)) if result else 0} 条记录")
    return {
        "db_result": result,
        "db_error": "",
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
