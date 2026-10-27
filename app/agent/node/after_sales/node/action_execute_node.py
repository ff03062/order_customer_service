"""
节点 7: action_execute_node（执行层）

职责：把最终决策落地成业务动作。

分工（重要）：
    工单  → 调 app/tool/mcp/ticket.py 的桩，返回写死的两行结果
    支付宝 → **真的**，但不在这个节点里调

为什么支付宝不在这里调？
    调 MCP 是异步的（网络请求），而本节点是同步函数。
    所以这里只标记 pending，真正的调用放在外层的 after_sales_node（异步）里做。
    所有真实外部调用集中在一处，以后加日志、加重试、加人工确认都只改那里。
"""
from app.tool.mcp import create_ticket

# 动作 → 工单标题
TICKET_TITLE = {
    "approve_return": "退货工单",
    "approve_exchange": "换货工单",
    "approve_repair": "维修工单",
}


def action_execute_node(state: dict) -> dict:
    """
    执行层。

    - 工单 / 通知：这里直接完成（工单是桩）
    - 退款 / 收款：只标记 pending，交给外层 after_sales_node 调支付宝
    """
    if not state.get("need_execute"):
        print("[action_execute] 无需执行，跳过")
        return {"execute_result": {}}

    payload = state.get("execute_payload") or {}
    tool = payload.get("tool", "")

    if tool in ("refund", "payment"):
        print(f"[action_execute] 标记支付宝动作 {tool}，order={payload.get('order_id')}")
        return {"execute_result": {"ok": True, "pending": True, "tool": tool}}

    if tool == "create_ticket":
        detail = create_ticket(
            str(payload.get("order_id", "")),
            TICKET_TITLE.get(payload.get("action", ""), "售后工单"),
        )
        result = {"ok": True, "tool": tool}
    else:
        detail = "已发送售后进度通知。"
        result = {"ok": True, "tool": tool}

    print(f"[action_execute] {detail}")
    solution = state.get("solution") or ""
    return {
        "execute_result": result,
        "solution": f"{solution}\n\n{detail}" if solution else detail,
    }

