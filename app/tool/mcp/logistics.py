"""
物流 MCP（桩）

真实场景这里应该像 alipay.py 一样连 SSE + call_tool，
现在按项目要求直接写死两行字符串代替结果，接口形态保持一致：
上层只认 `query_logistics(order_id) -> str`，将来换成真实 MCP 只改这一个文件。
"""


def query_logistics(order_id: str) -> str:
    """返回物流轨迹摘要（固定两行）。"""
    return (
        f"承运商：顺丰速运，运单号 SF{order_id}2026\n"
        f"最新状态：运输中，预计 1-2 天送达"
    )
