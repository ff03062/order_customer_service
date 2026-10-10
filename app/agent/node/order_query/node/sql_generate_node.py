"""
节点: sql_generate_node
职责：仅生成 SQL，不执行，不检查，不解析。

重试流程：
  - 首次调用：base prompt
  - 重试调用：追加 feedback（包含 db_error 和原语句）

输出：
  - raw_output: 模型原样输出的字符串（打印到控制台）
  - sql_attempts: 追加本次尝试记录
"""
from app.agent.model.my_model import MyModel


_SQL_PROMPT = """\
你是一个专业的电商订单查询SQL生成器。严格根据提供的表结构生成 SQL。

【数据库表结构（仅相关表）】
{schema_text}

【表关系速查】
- orders.user_id → 过滤用户数据
- orders.order_id ← order_items.order_id / logistics.order_id / after_sales.order_id
- order_items.product_id → products.id
- products.category_id → categories.id
- after_sales.user_id → 过滤用户数据

【SQL 生成规则】
- 只生成 SELECT 语句，禁止任何写操作
- 必须且只能使用提供的 Schema 中明确列出的字段名。
- 严禁根据常识自行推断、补全或修改字段名！
   - 错误示范：看到注释“快递公司”，就自行写成 `company_name`。
   - 正确示范：看到字段 `company` 注释为“快递公司”，必须严格使用 `company`。
- 如果用户询问的概念在 Schema 中没有完全对应的字段名，请寻找语义最接近的给定字段。
- 禁止 SELECT *，必须明确列出字段名
- orders / after_sales 表必须包含 user_id 过滤：WHERE user_id = {{user_id}}
- order_items / logistics 表通过 JOIN orders 过滤：WHERE o.user_id = {{user_id}}
- products / categories 是字典表，不需要 user_id 过滤
- 禁止 ORDER BY RAND()
- 禁止查询 users 表的任何字段
- 最多返回 100 条：加 LIMIT 100

【用户问题】
{user_input}

【输出格式】
必须严格输出以下 JSON，不要有任何其他内容：
{{"sql": "SELECT ..."}}"""

_RETRY_TPL = """

- 只允许修正字段名/表名/语法，禁止删除 WHERE 中的任何过滤条件
- 禁止添加 Schema 中不存在的字段
- 如果某个概念在 Schema 中找不到对应字段，请保留原过滤思路，改用语义最接近的字段

【上一次尝试的错误信息】
{db_error}

【上一次生成的 SQL】
{original_sql}

请根据以上错误信息修改 SQL（必须保留 {{user_id}} 占位符），只输出 JSON：
{{"sql": "改写后的 SQL"}}"""


def sql_generate_node(state: dict) -> dict:
    """
    生成 SQL。
    - 首次调用：base prompt
    - 重试调用：追加 feedback（包含 db_error 和原语句）
    """
    user_input = state["user_input"]
    schema_text = state.get("schema_text", "")
    sql_attempts: list = state.get("sql_attempts", [])
    db_error = state.get("db_error", "")
    original_sql = state.get("sql", "")

    prompt = _SQL_PROMPT.format(schema_text=schema_text, user_input=user_input)

    # 重试时追加反馈
    if db_error and original_sql:
        prompt += _RETRY_TPL.format(db_error=db_error, original_sql=original_sql)

    model = MyModel.get_model()
    raw_output = model.invoke(prompt).content

    # 打印到控制台
    print("=" * 60)
    print("[SQL 生成] 模型原始输出:")
    print(raw_output)
    print("=" * 60)

    return {
        "raw_output": raw_output,
        "sql_attempts": sql_attempts + [raw_output],
        "db_error": "",
    }
