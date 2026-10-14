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
    # 说明：current_order_id / order_context 是「无 reducer 的标量通道」，
    # 本轮输入没传时自动保留上一轮 checkpoint 的值 —— 这正是跨轮追问得以成立的基础。
    current_order_id: str | None  # 当前正在查的订单（订单/售后子图共用）
    current_order_ids: list[str]  # 会话中出现过的订单号（去重保序，最多 5 个）
    order_context: str  # 上下文描述，如"用户在查第3个订单，金额299元"
    sql: str  # 当前生成的 SQL（最新一次）
    sql_attempts: Annotated[list[str], operator.add]  # 每次尝试的 SQL（含失败的），便于调试
    db_result: str  # 数据库返回的 JSON 字符串
    db_error: str  # 数据库错误信息（空字符串表示成功）

    # ===== 售后咨询相关 =====
    retrieved_policies: list[str]  # rag 检索到的政策片段

    # ===== 售后决策子图相关 =====
    after_sales_result: dict  # 售后子图返回的结构化决策结果（可选，便于排查）

    # ===== 用户信息（登录后注入）=====
    user_id: str  # 用户ID
    user_email: str  # 用户邮箱

    # ===== 控制流 =====
    current_step: str  # "idle" | "waiting_order_id" | "showing_result" | "done"
    retry_count: int  # SQL 重试次数

    #  ===== 记忆相关（从外部存储读取，state 里只是引用）=====
    # session_summary: str  # 本次会话摘要（结束时写入 PostgreSQL）
    # user_profile_summary: str  # 用户画像摘要（从 PostgreSQL 读）
