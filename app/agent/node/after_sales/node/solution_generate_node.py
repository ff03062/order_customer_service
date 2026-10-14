"""
节点 5: solution_generate_node
职责：把结构化决策翻译成给用户看的自然语言答复，并锁定执行动作。

输入：intent / facts / order_facts / decision / policy_text / policy_basis
输出：solution / need_execute / execute_payload

设计要点：
    决策在这一步被「锁定」—— 无论它来自规则引擎还是 LLM 灰区推理，
    执行动作（need_execute / execute_payload）都在这里统一计算，
    避免上游两条路径各算一遍导致不一致。
"""
import json

from langchain_core.messages import HumanMessage

from app.agent.model.my_model import MyModel
from app.agent.node.after_sales.schema import INTENT_LABELS

# 决策动作 → 是否需要触发执行，以及调用哪个工具
ACTION_TO_TOOL = {
    "approve_return": "create_ticket",
    "approve_exchange": "create_ticket",
    "approve_repair": "create_ticket",
}

# 只通知、不产生新业务单据的动作
NOTIFY_ONLY_ACTIONS = {"notify_existing"}

_SYSTEM_PROMPT = """你是电商平台的售后客服，负责把已经确定的处理结论转述给用户。

【表达规则】
1. 用简洁、友好、口语化的中文，控制在 3~5 句话
2. 必须告知：能不能办、依据是什么（用条款编号对应的政策内容表述，不要直接输出编号）、
   下一步怎么做、大概多久
3. 涉及金额时保留两位小数
4. 不要输出 Markdown 代码块，不要输出"依据条款 POLICY-XX"这种内部编号
5. 不要承诺政策里没有的内容；结论是 reject 时，要给出可行的替代方案或联系方式
6. 结论里有"运费承担"说明时，必须用一句话告知用户运费由谁承担
7. 只输出给用户看的话，不要任何前缀说明"""


def _format_facts(order_facts: dict) -> str:
    if not order_facts or not order_facts.get("found"):
        return "（未取到订单事实，请按信息不足处理并引导用户核对订单号）"

    is_signed = bool(order_facts.get("is_signed"))
    days = order_facts.get("days_since_receipt", -1)
    if is_signed:
        signed_desc = (
            f"已签收，签收距今 {days} 天"
            f"（依据：{order_facts.get('signed_source') or '未知'}）"
        )
    else:
        signed_desc = (
            f"**未签收**（物流状态 {order_facts.get('logistics_status')}，"
            f"依据：{order_facts.get('signed_source') or '未知'}）"
            "—— 商品在途中，七天无理由时效尚未开始，用户可随时退货"
        )

    return (
        f"订单状态：{order_facts.get('order_status')}，"
        f"{signed_desc}，"
        f"下单距今 {order_facts.get('days_since_purchase')} 天，"
        f"商品品类：{order_facts.get('categories')}，"
        f"实付金额：{order_facts.get('pay_amount')}，"
        f"运费险：{'有' if order_facts.get('has_freight_insurance') else '无'}，"
        f"已有售后单：{'是' if order_facts.get('has_existing_after_sales') else '否'}"
    )


def _compute_execute(decision: dict, state: dict) -> tuple[bool, dict]:
    """按最终决策计算是否需要执行，以及执行参数。"""
    action = decision.get("action", "consult")

    if action in NOTIFY_ONLY_ACTIONS:
        # 已有售后单：只需通知，不能重复建单
        facts = state.get("facts") or {}
        order_facts = state.get("order_facts") or {}
        return True, {
            "tool": "notify",
            "action": action,
            "order_id": str(facts.get("order_id") or order_facts.get("order_id") or ""),
            "user_id": str(state.get("user_id") or ""),
            "reason_code": decision.get("reason_code", ""),
        }

    tool = ACTION_TO_TOOL.get(action)
    if not tool:
        return False, {}

    facts = state.get("facts") or {}
    order_facts = state.get("order_facts") or {}
    return True, {
        "tool": tool,
        "action": action,
        "order_id": str(facts.get("order_id") or order_facts.get("order_id") or ""),
        "user_id": str(state.get("user_id") or ""),
        "refund_amount": float(decision.get("refund_amount") or 0.0),
        "freight_compensation": float(decision.get("freight_compensation") or 0.0),
        "reason_code": decision.get("reason_code", ""),
        "clause_ids": (state.get("policy_basis") or [])[:3],
    }


def _fallback_solution(decision: dict, state: dict, need_human: bool) -> str:
    """模型不可用时的确定性兜底话术（保证任何情况下都有输出）。"""
    # 原因码可能来自规则决策，也可能来自 LLM 复核结果
    reason_code = decision.get("reason_code", "")
    if not reason_code:
        rule_summary = (state.get("rule_result") or {}).get("summary") or {}
        reason_code = rule_summary.get("reason_code", "")

    if reason_code == "EXISTING_AFTER_SALES":
        return ("这笔订单已经有一笔售后申请正在处理中，您可以在「我的订单 - 售后详情」查看进度，"
                "无需重复提交；如需加急或有新问题，我可以帮您转接人工客服。")

    # 运费承担说明由规则层给出，兜底时也要带上（模型不可用不能丢这个信息）
    freight_note = decision.get("freight_note") or ""
    freight_suffix = f"\n{freight_note}。" if freight_note else ""

    action = decision.get("action", "consult")
    amount = decision.get("refund_amount") or 0.0
    if need_human or action == "consult":
        return ("您的情况我这边需要人工客服进一步核实，已为您记录，"
                "稍后会有专员跟进；您也可以直接致电 400-123-4567（9:00-21:00）。")
    if action in ("approve_return", "notify_existing"):
        return (f"已为您受理退货申请，退款金额 {amount:.2f} 元，"
                "请在订单详情页提交退货并寄回商品，我们收到并确认后 3-5 个工作日内原路退回。"
                + freight_suffix)
    if action == "approve_exchange":
        return ("已为您受理换货申请，请在订单详情页提交换货并寄回商品，"
                "我们收到后会尽快为您寄出。" + freight_suffix)
    if action == "approve_repair":
        return ("已为您受理维修申请，请在订单详情页提交维修并寄送商品至指定地址，"
                "维修完成后我们会寄回给您。" + freight_suffix)
    return ("抱歉，这笔订单暂时无法直接办理该售后申请，"
            "您可以联系在线客服或致电 400-123-4567 进一步核实。")


def solution_generate_node(state: dict) -> dict:
    decision = state.get("decision") or {}
    need_human = bool(state.get("need_human"))

    # 订单号已提供但事实取不到（查库失败 / 订单不属于该用户）：
    # 不能凭规则胡答，直接转人工，并明确告知原因
    facts = state.get("facts") or {}
    order_facts = state.get("order_facts") or {}
    if facts.get("order_id") and not order_facts.get("found"):
        reason = order_facts.get("error") or state.get("db_error") or "订单信息暂时无法获取"
        if "不存在" in str(reason) or "不属于" in str(reason):
            solution = ("抱歉，我没有查到这个订单号对应的订单，"
                        "麻烦您核对一下订单号，或提供下单手机号，我帮您再查一次～")
        else:
            solution = ("抱歉，订单系统暂时无法访问，我这边没法确认这笔订单的情况。"
                        "已为您记录，稍后会有专员跟进；您也可以致电 400-123-4567（9:00-21:00）。")
        print(f"[solution_generate] 订单事实缺失({reason})，转人工兜底")
        return {
            "solution": solution,
            "need_execute": False,
            "execute_payload": {},
            "need_human": True,
        }

    need_execute, execute_payload = _compute_execute(decision, state)
    # 转人工时不自动执行业务动作（避免误退款/误换货）；
    # 但「已有售后单」这类纯通知动作可以照常发送
    if need_human and decision.get("action") not in NOTIFY_ONLY_ACTIONS:
        need_execute = False

    solution = ""
    prompt = _SYSTEM_PROMPT + (
        f"\n\n【用户诉求】{INTENT_LABELS.get(state.get('intent', ''), state.get('intent', ''))}"
        f"\n【用户原话】{state.get('rewritten_input') or state.get('user_input', '')}"
        f"\n【订单事实】{_format_facts(state.get('order_facts') or {})}"
        f"\n【最终结论】{json.dumps(decision, ensure_ascii=False)}"
        f"\n【命中政策】\n{state.get('policy_text') or '（知识库未覆盖）'}"
        f"\n【是否转人工】{'是' if need_human else '否'}"
    )

    for getter in ("get_model", "get_local_model"):
        try:
            model = getattr(MyModel, getter)()
            text = model.invoke([HumanMessage(content=prompt)]).content
            if text and isinstance(text, str) and text.strip():
                solution = text.strip()
                break
        except Exception as e:
            print(f"[solution_generate] {getter} 调用失败: {e}")

    if not solution:
        print("[solution_generate] 模型不可用，使用兜底话术")
        solution = _fallback_solution(decision, state, need_human)

    print(f"[solution_generate] need_execute={need_execute} solution={solution[:60]}...")

    return {
        "solution": solution,
        "need_execute": need_execute,
        "execute_payload": execute_payload,
    }
