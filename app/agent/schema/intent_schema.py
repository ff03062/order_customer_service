"""
意图识别 Pydantic Schema
"""
from pydantic import BaseModel, Field
from typing import Literal


class IntentSchema(BaseModel):
    """意图识别输出格式"""

    intent: Literal["order_query", "after_sales", "chat", "unknown"] = Field(
        description="用户意图：order_query=订单查询, after_sales=售后咨询, chat=闲聊, unknown=未知"
    )

    detail: str = Field(
        default="",
        description="意图详情，如提取到的订单号"
    )

# class RouterSchema(BaseModel):
#     route: str = Field(..., description="路由分类，只在exam,answer,chat三个中一个")