"""
MySQL 工具 - 含 LangChain Tool

对外暴露两级能力：
  1. `run_readonly_query(sql, user_id)` —— 纯函数形式的只读查询，供内部代码直接调用
     （售后事实聚合等「固定查询」场景使用，不经过 LLM）
  2. `mysql_tool` —— LangChain Tool 封装，供 Agent 自主调用（Text2SQL 场景）
"""
import datetime
import json
import re

from langchain.tools import tool
import pymysql
import os
from dotenv import load_dotenv

from app.tool.schema.mysql_schema import MySQLSchema

load_dotenv()

# 禁止写操作：DELETE / UPDATE / INSERT / DROP / ALTER / TRUNCATE
# 用单词边界正则，避免 l.updated_at、create_update_time 等字段名误判
DANGEROUS_PATTERN = re.compile(
    r"\b(DELETE|UPDATE|INSERT|DROP|ALTER|TRUNCATE)\b",
    re.IGNORECASE,
)

# 只读白名单：必须 SELECT / WITH 开头
READONLY_PREFIX_PATTERN = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def db_kwargs(db_name: str) -> dict:
    """
    选择连接参数名。

    新版 pymysql 用 `database=`，旧版只有 `db=`；
    传旧参数在新版上会触发 DeprecationWarning，所以这里按版本挑。
    """
    try:
        parts = tuple(int(x) for x in pymysql.__version__.split(".")[:2])
        return {"database": db_name} if parts >= (1, 0) else {"db": db_name}
    except Exception:
        return {"database": db_name}


def run_readonly_query(sql: str, user_id=None) -> str:
    """
    执行只读查询并返回 JSON 字符串。

    安全约束（与 mysql_tool 完全一致）：
        - 必须以 SELECT / WITH 开头
        - 禁止 DELETE / UPDATE / INSERT / DROP / ALTER / TRUNCATE
        - 支持 {user_id} 占位符替换

    返回：
        - 成功：JSON 数组字符串，例如 '[{"order_id": 1}]'
        - 失败：纯文本错误信息（调用方按「非 JSON」判定为失败）
    """
    con = None
    cursor = None
    try:
        host = os.getenv("DB_HOST")
        port = os.getenv("DB_PORT")
        user = os.getenv("DB_USER")
        password = os.getenv("DB_PASSWORD")
        db_name = os.getenv("DB_NAME")

        if not sql or not sql.strip():
            return "SQL 为空"

        if not host or not port or not user or not password or not db_name:
            return "数据库连接参数未配置"

        if not READONLY_PREFIX_PATTERN.match(sql):
            return "只允许 SELECT / WITH 开头的查询语句"

        # 兜底操作：用单词边界正则匹配，避免字段名误判
        if DANGEROUS_PATTERN.search(sql):
            return "禁止执行非法sql语句操作!"

        # 替换用户ID
        if user_id not in (None, ""):
            sql = sql.replace("{user_id}", str(user_id))

        con = pymysql.connect(
            host=host,
            port=int(port),
            user=user,
            password=password,
            **db_kwargs(db_name),
            charset="utf8",
            cursorclass=pymysql.cursors.DictCursor,
        )
        cursor = con.cursor()
        # 执行sql
        cursor.execute(sql)
        rs = cursor.fetchall()
        return json.dumps(rs, ensure_ascii=False, default=str)

    except Exception as e:
        print(f"[run_readonly_query] {e}")
        return str(e)

    finally:
        if cursor:
            cursor.close()
        if con:
            con.close()


@tool(args_schema=MySQLSchema)
def mysql_tool(sql: str, user_id: int) -> str:
    """
     执行 SELECT 查询语句，返回 JSON 格式结果。

    数据库：order_db（电商订单库）
    包含表：users / categories / products / orders / order_items / logistics / after_sales

    规则：
        - 只允许 SELECT / WITH 开头的查询
        - 禁止 DELETE / UPDATE / INSERT / DROP / ALTER / TRUNCATE 等写操作
        - 查询 orders / after_sales 会自动带上 user_id 过滤
        - 最多返回 100 行
    """
    return run_readonly_query(sql, user_id)


def to_datetime(value):
    """把数据库返回的时间字段统一成 datetime（无法解析时返回 None）。"""
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(value[:19], fmt)
            except ValueError:
                continue
    return None


if __name__ == '__main__':
    print(mysql_tool.invoke({
        "sql": f"SELECT * FROM order_db.users WHERE user_id = 1;",
        "user_id": 1
    }))
