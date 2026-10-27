"""
工单 MCP（桩）

真实场景这里应该调工单系统的 MCP 工具，现在按项目要求写死两行字符串代替结果。
上层只认 `create_ticket(order_id, title) -> str`。
"""


def create_ticket(order_id: str, title: str) -> str:
    """创建一张售后工单（固定两行）。"""
    return (
        f"工单「{title}」已创建，工单号 TK{order_id}2026\n"
        f"客服会在 24 小时内联系您，请保持电话畅通"
    )
