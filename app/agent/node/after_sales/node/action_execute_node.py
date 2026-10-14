"""
节点 7: action_execute_node
职责：决策落地后的业务动作执行（MCP 桩实现）。

当前为**本地桩**：所有动作都返回模拟结果，不访问外部系统，
方便在无物流/支付/工单系统的情况下完整跑通"决策 → 执行"链路。

真实接入时，把 `_call_tool` 换成 MCP Client 即可（接口形态已经按 MCP 设计）：

    ┌──────────────────┐        MCP (stdio / HTTP)        ┌────────────────┐
    │  客服 Agent       │ ───────────────────────────────► │ 物流 MCP Server │
    │ action_execute   │ ───────────────────────────────► │ 支付 MCP Server │
    └──────────────────┘ ───────────────────────────────► │ 工单 MCP Server │
                                                           └────────────────┘

安全约定（真实接入时必须保留）：
    1. 写操作（退款/换货）必须二次确认，不能由模型直接触发
    2. 每个请求带幂等键 idempotency_key = user_id + order_id + action，防重复退款
    3. 超时 + 失败重试 1 次；仍失败则降级为「已记录，待人工处理」
"""
import time
import uuid

# 允许执行的动作白名单（无白名单里的动作一律不执行）
ALLOWED_TOOLS = {
    "create_ticket": "创建售后工单",
    "apply_refund": "发起退款",
    "query_logistics": "查询物流",
    "notify": "发送通知",
}


def _idempotency_key(payload: dict) -> str:
    return f"{payload.get('user_id', '')}:{payload.get('order_id', '')}:{payload.get('action', '')}"


def _call_tool(tool: str, payload: dict) -> dict:
    """
    调用工具（当前为本地桩）。

    真实实现示意：
        from mcp import ClientSession
        async with ClientSession(...) as session:
            return await session.call_tool(tool, arguments=payload)
    """
    if tool not in ALLOWED_TOOLS:
        return {"ok": False, "error": f"未授权的工具：{tool}"}

    ticket_id = "TK" + uuid.uuid4().hex[:10].upper()
    order_id = payload.get("order_id", "")

    if tool == "create_ticket":
        action = payload.get("action", "")
        amount = float(payload.get("refund_amount") or 0.0)
        if action == "approve_return":
            detail = f"退货工单已创建，退款金额 {amount:.2f} 元，等待寄回商品"
        elif action == "approve_exchange":
            detail = "换货工单已创建，等待寄回商品"
        else:
            detail = "维修工单已创建，等待寄送商品"
        return {"ok": True, "tool": tool, "ticket_id": ticket_id, "detail": detail,
                "clause_ids": payload.get("clause_ids", [])}

    if tool == "notify":
        return {"ok": True, "tool": tool, "detail": "已发送售后进度通知"}

    if tool == "apply_refund":
        amount = float(payload.get("refund_amount") or 0.0)
        return {"ok": True, "tool": tool, "detail": f"已发起退款 {amount:.2f} 元，3-5 个工作日到账"}

    if tool == "query_logistics":
        return {"ok": True, "tool": tool, "detail": f"订单 {order_id} 物流状态已查询"}

    return {"ok": False, "error": "未实现的动作"}


def action_execute_node(state: dict) -> dict:
    if not state.get("need_execute"):
        print("[action_execute] 无需执行动作，跳过")
        return {"execute_result": {}}

    payload = state.get("execute_payload") or {}
    tool = payload.get("tool", "")
    payload = {**payload, "idempotency_key": _idempotency_key(payload)}

    print(f"[action_execute] 调用 MCP 工具: {tool} payload={payload}")

    started = time.time()
    try:
        result = _call_tool(tool, payload)
    except Exception as e:
        result = {"ok": False, "error": str(e)}
    latency_ms = int((time.time() - started) * 1000)

    result["latency_ms"] = latency_ms
    solution = state.get("solution") or ""

    if result.get("ok"):
        suffix = result.get("detail", "")
        if result.get("ticket_id"):
            suffix += f"（工单号 {result['ticket_id']}）"
        print(f"[action_execute] 执行成功: {suffix}")
        return {
            "execute_result": result,
            "solution": f"{solution}\n\n{suffix}。" if solution else suffix,
        }

    # 执行失败：降级为人工处理，不向用户暴露技术细节
    print(f"[action_execute] 执行失败: {result.get('error')}")
    fallback = "您的申请已记录，我会为您转交人工客服跟进，请留意后续通知。"
    return {
        "execute_result": result,
        "solution": f"{solution}\n\n{fallback}" if solution else fallback,
        "need_human": True,
    }


if __name__ == "__main__":
    demo = {
        "need_execute": True,
        "solution": "已为您受理退货申请。",
        "execute_payload": {
            "tool": "create_ticket",
            "action": "approve_return",
            "order_id": "1",
            "user_id": "1",
            "refund_amount": 299.0,
        },
    }
    print(action_execute_node(demo))
