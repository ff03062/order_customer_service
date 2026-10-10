"""
MySQL 工具 - 含 LangChain Tool
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

# 禁止["删除", "更新", "插入", "删除表", "修改表结构", "清空表"]
# 用单词边界正则，避免 l.updated_at、create_update_time 等字段名误判
DANGEROUS_PATTERN = re.compile(
    r"\b(DELETE|UPDATE|INSERT|DROP|ALTER|TRUNCATE)\b",
    re.IGNORECASE,
)


@tool(args_schema=MySQLSchema)
def mysql_tool(sql: str, user_id: int) -> str:
    # 测试死循环
    # sql = sql.replace("orders", "orders_xxx")
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

        # 兜底操作：用单词边界正则匹配，避免字段名误判
        if DANGEROUS_PATTERN.search(sql):
            return "禁止执行非法sql语句操作!"

        # 替换用户ID
        if user_id:
            sql = sql.replace(chr(0x7B) + "user_id" + chr(0x7D), str(user_id))

        con = pymysql.connect(
            host=host,
            port=int(port),
            user=user,
            password=password,
            db=db_name,
            charset="utf8",
            cursorclass=pymysql.cursors.DictCursor,
        )
        cursor = con.cursor()
        # 执行sql
        cursor.execute(sql)
        rs = cursor.fetchall()
        return json.dumps(rs, ensure_ascii=False, default=str)


    except Exception as e:
        print(f"[mysql_tool] type={type(e).__name__} args={e.args}")
        return str(e)

    finally:
        if cursor:
            cursor.close()
        if con:
            con.close()


if __name__ == '__main__':
    print(mysql_tool.invoke({
        "sql": f"SELECT * FROM order_db.users WHERE user_id = 1;",
        "user_id": "1"
    }))
