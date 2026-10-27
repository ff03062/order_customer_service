"""
订单查询子图专用 State

注意：这是子图内部的状态，与主图 CustomerServiceState 分离。
子图入口/出口做转换。
"""
import operator
from typing import Annotated
from typing_extensions import TypedDict


class OrderQueryState(TypedDict):
    """
    订单查询子图的状态

    字段流向：
      intent_classify_node   → intent
      schema_select_node     → relevant_tables, relevant_fields, schema_text
      sql_generate_node      → raw_output, sql_attempts
      sql_check_node         → sql, sql_check_result, retry_count, db_error
      sql_execute_node       → db_result / db_error
      answer_translate_node  → answer

    控制流（条件边）：
      sql_generate → sql_check
                         ├─ "invalid_json" / "empty" / "dangerous" / "no_user_id" → sql_generate（重试）
                         ├─ "exhausted"      → answer_translate_node
                         └─ "pass"           → sql_execute
      sql_execute → sql_check
                         ├─ db_error → sql_generate（重试）
                         └─ 成功     → answer_translate_node → END
    """

    # === 输入 ===
    user_input: str
    user_id: str

    # === 上下文（由主图 order_node 注入，用于省略指代消解）===
    chat_history: list           # 最近几轮对话（LangChain Message 对象列表）
    last_order_id: str           # 上一轮解析出的订单号（来自主图 current_order_id）
    order_context: str           # 上一轮在聊什么
    rewritten_input: str         # query_rewrite_node 改写后的自包含问题
    resolved_order_ids: list     # 本次查询实际命中的订单号（供主图回写）

    # === 中间产物 ===
    intent: str                  # "query_orders" | "query_order_items" | "query_logistics" | "query_after_sales" | "query_products" | "chitchat"
    relevant_tables: list[str]   # 例如 ["orders", "logistics"]
    relevant_fields: dict        # 例如 {"orders": ["order_id", "status"]}
    schema_text: str             # 渲染后的 schema 文本

    raw_output: str              # 模型原样输出的字符串（打印到控制台）
    sql: str                     # 解析后的 SQL
    sql_attempts: Annotated[list[str], operator.add]  # 每次尝试的原始输出
    sql_check_result: str        # "invalid_json" | "empty" | "no_user_id" | "dangerous" | "pass" | "exhausted" | ""（初始为空）
    db_result: str               # 数据库返回的 JSON 字符串（成功时）
    db_error: str                # 检查/执行失败信息（成功时为空）
    retry_count: int             # SQL 重试次数

    # === 输出（由 answer_translate_node 写入）===
    answer: str                  # 最终给用户的中文回答
