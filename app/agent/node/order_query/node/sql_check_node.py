"""
节点 3.5: sql_check_node
职责：检查 SQL 是否合格，决定下一步走向。

决策（写入 state.sql_check_result）：
  - "invalid_json"  ：JSON 解析失败（剥 markdown / json.loads / 取 sql 字段 任一步失败）
  - "empty"        ：SQL 字段为空
  - "no_user_id"   ：缺少 user_id 占位符（字典表除外）
  - "dangerous"    ：检测到危险 SQL
  - "pass"         ：通过，去执行
  - "exhausted"    ：重试次数超限
"""
import json
import re

# 字典表意图，不需要 user_id 过滤的表
DICT_INTENTS = {"query_products", "chitchat"}

# 最大重试次数
MAX_RETRIES = 3

# 危险 SQL 模式（必须独立成行，排除注释内的）
DANGEROUS_PATTERNS = [
    (re.compile(r"^\s*SELECT\s+\*\s", re.MULTILINE | re.IGNORECASE), "禁止 SELECT * 全选"),
    (re.compile(r"^\s*(DELETE|UPDATE|INSERT|DROP|ALTER|TRUNCATE)\b", re.MULTILINE | re.IGNORECASE), "禁止写操作"),
]


def strip_markdown(text: str) -> str:
    """剥掉 markdown 代码块。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # 去掉第一行的 ```json 或 ```sql 等
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        # 去掉最后一行的 ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def parse_json_output(raw: str) -> dict | None:
    """解析 JSON 输出，返回 dict 或 None。"""
    text = strip_markdown(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[JSON 解析失败] {e}")
        return None


def check_dangerous_sql(sql: str) -> str | None:
    """
    检查危险 SQL，返回错误信息或 None。
    只检测 SELECT * 全选和写操作，不检测注释和分号。
    """
    if not sql:
        return "SQL 为空"
    for pattern, msg in DANGEROUS_PATTERNS:
        if pattern.search(sql):
            print(f"[危险 SQL 检测] {msg}: {sql}")
            return msg
    return None


def has_user_id_placeholder(sql: str) -> bool:
    """检查是否包含 user_id 占位符。"""
    if not sql:
        return False
    return bool(
        re.search(r"\b\w+\.user_id\s*=\s*\{user_id\}", sql, re.IGNORECASE)
        or re.search(r"\buser_id\s*=\s*\{user_id\}", sql, re.IGNORECASE)
    )


# LIMIT / ORDER BY / GROUP BY / HAVING 必须排在 WHERE 之后，
# 所以补 user_id 时要插在它们前面，不能直接追加到 SQL 末尾
TRAILING_CLAUSE_PATTERN = re.compile(
    r"\b(LIMIT|ORDER\s+BY|GROUP\s+BY|HAVING)\b", re.IGNORECASE
)


def _insert_condition(sql: str, condition: str) -> str:
    """
    把过滤条件插到正确的位置。

    正确：SELECT ... WHERE order_id = 1 AND user_id = {{user_id}} LIMIT 100
    错误：SELECT ... WHERE order_id = 1 LIMIT 100 AND user_id = {{user_id}}
          （AND 跑到 LIMIT 后面，直接 1064 语法错误）
    """
    match = TRAILING_CLAUSE_PATTERN.search(sql)
    if not match:
        return sql + " " + condition

    head = sql[: match.start()].rstrip()
    tail = sql[match.start():].strip()

    joiner = " AND " if re.search(r"\bWHERE\b", head, re.IGNORECASE) else " WHERE "
    return f"{head}{joiner}{condition} {tail}"


def supplement_user_id(sql: str, intent: str, tables: list[str]) -> str:
    """
    补充 user_id 占位符。
    - orders / after_sales 表：加 user_id = {user_id} 过滤
    - 字典表（products / categories）：不需要过滤
    - 会自动插到 LIMIT / ORDER BY 之前，避免语法错误
    """
    if intent in DICT_INTENTS:
        return sql

    if "orders" in tables and not has_user_id_placeholder(sql):
        sql = _insert_condition(sql, "user_id = {user_id}")

    return sql


def sql_check_node(state: dict) -> dict:
    """
    检查 SQL 合规性，写入 sql_check_result。
    外部通过条件边决定是否回流重试。
    """
    raw_output = state.get("raw_output", "")
    intent = state.get("intent", "query_orders")
    relevant_tables: list[str] = state.get("relevant_tables", [])
    retry_count = state.get("retry_count", 0)

    # ---- 1. JSON 解析 ----
    obj = parse_json_output(raw_output)
    if obj is None:
        new_count = retry_count + 1
        if new_count >= MAX_RETRIES:
            return {
                "sql_check_result": "exhausted",
                "retry_count": new_count,
                "sql": "",
                "db_error": f"JSON 解析失败（{MAX_RETRIES} 次重试后），请稍后再试或联系人工客服。",
            }
        return {
            "sql_check_result": "invalid_json",
            "retry_count": new_count,
            "sql": "",
            "db_error": f"模型输出无法解析为 JSON，请重新生成。原始输出：{raw_output[:200]}",
        }

    # ---- 2. 取 sql 字段 ----
    sql = obj.get("sql", "")

    # ---- 3. 空值判断 ----
    if not sql or not sql.strip():
        new_count = retry_count + 1
        if new_count >= MAX_RETRIES:
            return {
                "sql_check_result": "exhausted",
                "retry_count": new_count,
                "sql": "",
                "db_error": "SQL 字段为空，已重试 3 次仍失败，请稍后再试或联系人工客服。",
            }
        return {
            "sql_check_result": "empty",
            "retry_count": new_count,
            "sql": "",
            "db_error": "SQL 字段为空，请重新生成。",
        }

    sql = sql.strip()

    # ---- 4. 防注入检查 ----
    dangerous_msg = check_dangerous_sql(sql)
    if dangerous_msg:
        new_count = retry_count + 1
        if new_count >= MAX_RETRIES:
            return {
                "sql_check_result": "exhausted",
                "retry_count": new_count,
                "sql": "",
                "db_error": f"SQL 安全检查失败（{MAX_RETRIES} 次重试后）：{dangerous_msg}",
            }
        return {
            "sql_check_result": "dangerous",
            "retry_count": new_count,
            "sql": sql,  # 保留 SQL 供重试时参考
            "db_error": f"SQL 安全检查失败：{dangerous_msg}，请重新生成。",
        }

    # ---- 5. user_id 占位符检查 ----
    if intent not in DICT_INTENTS and not has_user_id_placeholder(sql):
        # 尝试补充
        sql = supplement_user_id(sql, intent, relevant_tables)
        if not has_user_id_placeholder(sql):
            new_count = retry_count + 1
            if new_count >= MAX_RETRIES:
                return {
                    "sql_check_result": "exhausted",
                    "retry_count": new_count,
                    "sql": "",
                    "db_error": f"SQL 缺少 user_id 占位符，已重试 {MAX_RETRIES} 次仍不符合要求。",
                }
            return {
                "sql_check_result": "no_user_id",
                "retry_count": new_count,
                "sql": sql,  # 保留 SQL 供重试时参考
                "db_error": f"SQL 缺少 user_id = {{{{user_id}}}} 占位符，请补全后重试。",
            }

    # ---- 通过 ----
    print("=" * 60)
    print("[SQL 检查通过]")
    print(f"意图: {intent}")
    print(f"SQL: {sql}")
    print("=" * 60)

    return {
        "sql_check_result": "pass",
        "sql": sql,
        # "retry_count": 0,
        "db_error": "",
    }
