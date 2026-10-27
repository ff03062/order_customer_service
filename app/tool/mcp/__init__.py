"""
MCP 集成层

支付走阿里云百炼 MCP 广场的真实支付宝服务；物流、工单目前是桩（写死两行字符串）。

对外只暴露这些名字，上层不直接碰内部文件：
    AlipayMCP        支付宝 MCP 客户端（async with 使用）
    make_trade_no    生成商户订单号
    add_record       交易落库（防重复退款）
    last_trade_no    取回原交易号
    query_logistics  物流查询（桩）
    create_ticket    建售后工单（桩）
"""
from app.tool.mcp.alipay import AlipayMCP, make_trade_no
from app.tool.mcp.logistics import query_logistics
from app.tool.mcp.record import add_record, last_trade_no
from app.tool.mcp.ticket import create_ticket

__all__ = [
    "AlipayMCP",
    "make_trade_no",
    "add_record",
    "last_trade_no",
    "query_logistics",
    "create_ticket",
]
