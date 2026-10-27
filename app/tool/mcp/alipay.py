"""
支付宝 MCP 客户端

连的是阿里云百炼 MCP 广场的支付宝服务（SSE 长连接），不是自己写的 MCP Server。

用法：
    async with AlipayMCP() as ap:
        await ap.create_payment("1", 5.0, "退货运费")   # 创建收款单，返回支付链接
        await ap.query_payment("FRT1...")              # 查付款状态
        await ap.refund("FRT1...", 5.0, "1", "测试")    # 退款

注意：refund() 的 outRequestNo 是幂等号，重复用同一个号只会退一次。
      但本模块每次调用都会生成**新号**，所以重试前必须从 record.py 里取回原号。
"""
import os
import time

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.sse import sse_client

load_dotenv()

MCP_URL = "https://dashscope.aliyuncs.com/api/v1/mcps/alipay-ty/sse"
API_KEY = os.getenv("DASHSCOPE_API_KEY")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}   # 百炼鉴权格式，Bearer 后有空格


def make_trade_no(prefix: str, order_id: str) -> str:
    """
    生成商户订单号（支付宝靠它认单）。

    不能直接用业务订单号，因为同一订单可能既要收运费又要退款，会撞车。
    前缀区分业务：FRT=收运费，RFD=退款；时间戳保证唯一。
    """
    return f"{prefix}{order_id}{int(time.time() * 1000)}"


def _pick_text(result) -> str:
    """把 MCP 返回的 content 数组拼成纯文本。"""
    return "\n".join(
        item.text for item in (getattr(result, "content", None) or [])
        if getattr(item, "text", None)
    )


class AlipayMCP:
    """支付宝 MCP 客户端。连接是长连接，用 async with 自动管理开关。"""

    async def __aenter__(self):
        """连接：建立 SSE → 握手 → 记录可用工具。"""

        self._conn_ctx = sse_client(MCP_URL, headers=HEADERS, timeout=30, sse_read_timeout=300)
        read, write = await self._conn_ctx.__aenter__()

        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()

        await self._session.initialize()                      # ① 握手

        listed = await self._session.list_tools()             # ② 问有哪些能力
        self.tool_names = [t.name for t in listed.tools]
        return self

    async def __aexit__(self, *exc):
        """断开连接。"""
        await self._session_ctx.__aexit__(*exc)
        await self._conn_ctx.__aexit__(*exc)

    async def _call(self, tool: str, args: dict) -> str:
        """③ 调用工具的统一入口，返回纯文本。"""
        result = await self._session.call_tool(tool, args)
        return _pick_text(result)

    # ==================== 业务方法 ====================

    async def create_payment(self, order_id: str, amount: float,
                             title: str = "退货运费", mobile: bool = False) -> str:
        """
        创建收款单，返回带支付链接的 Markdown 文本。

        amount 是数字、单位元。mobile=True 用手机端接口，否则用网页端（出二维码）。
        """
        return await self._call(
            "create-mobile-alipay-payment" if mobile else "create-web-page-alipay-payment",
            {
                "outTradeNo": make_trade_no("FRT", order_id),
                "totalAmount": amount,
                "orderTitle": title,
            },
        )

    async def query_payment(self, trade_no: str) -> str:
        """查这笔收款单付款状态。"""
        return await self._call("query-alipay-payment", {"outTradeNo": trade_no})

    async def refund(self, trade_no: str, amount: float,
                     order_id: str, reason: str = "售后退款") -> str:
        """退款给用户。"""
        return await self._call("refund-alipay-payment", {
            "outTradeNo": trade_no,
            "refundAmount": amount,
            "outRequestNo": make_trade_no("RFD", order_id),
            "refundReason": reason,
        })

    async def query_refund(self, trade_no: str, request_no: str) -> str:
        """查退款到账状态。"""
        return await self._call("query-alipay-refund", {
            "outTradeNo": trade_no,
            "outRequestNo": request_no,
        })


if __name__ == "__main__":
    import asyncio

    async def smoke_test():
        """冒烟测试：连上 → 打印工具列表 → 创建一笔 5 元收款单。"""
        async with AlipayMCP() as ap:
            print("可用工具：", ap.tool_names)
            print(await ap.create_payment("1", 5.0, "退货运费补付"))

    asyncio.run(smoke_test())
