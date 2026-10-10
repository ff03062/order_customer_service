"""
CustomerService 状态定义
"""
import operator
from typing import Annotated

from typing_extensions import TypedDict
from langchain.messages import AnyMessage


class CustomerServiceState(TypedDict):
    """
    客服系统状态定义

    分层存储策略：
    - 窗口记忆与用户画像存 Redis List
    - 会话摘要：存在 PostgreSQL
    - 长期记忆：存chromadb
    """

    # ===== 即时上下文（内存）=====

    # 消息历史（自动追加）
    messages: Annotated[list[AnyMessage], operator.add]

    # 意图识别结果
    intent: str  # "order_query" | "after_sales" | "chat" | "unknown"
    detail: str  # 意图判断依据或补充信息

    # ===== 订单查询相关 =====
    current_order_id: str | None  # 当前正在查的订单
    order_context: str  # 上下文描述，如"用户在查第3个订单，金额299元"
    sql: str  # 当前生成的 SQL（最新一次）
    sql_attempts: Annotated[list[str], operator.add]  # 每次尝试的 SQL（含失败的），便于调试
    db_result: str  # 数据库返回的 JSON 字符串
    db_error: str  # 数据库错误信息（空字符串表示成功）

    # ===== 售后咨询相关 =====
    retrieved_policies: list[str]  # rag 检索到的政策片段

    # ===== 用户信息（登录后注入）=====
    user_id: str  # 用户ID
    user_email: str  # 用户邮箱

    # ===== 控制流 =====
    current_step: str  # "idle" | "waiting_order_id" | "showing_result" | "done"
    retry_count: int  # SQL 重试次数

    #  ===== 记忆相关（从外部存储读取，state 里只是引用）=====
    # session_summary: str  # 本次会话摘要（结束时写入 PostgreSQL）
    # user_profile_summary: str  # 用户画像摘要（从 PostgreSQL 读）
