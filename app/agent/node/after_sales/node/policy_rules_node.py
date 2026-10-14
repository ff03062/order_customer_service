"""
节点 3: policy_rules_node
职责：政策检索 + 规则判定（确定性判定的落点）

流程：
    ① 政策检索（向量 + BM25 + RRF，命中即记录 clause_id 作为决策依据）
    ② 遍历规则表，按 cond 判断适用性、按 check 判定通过与否
    ③ 合并规则结果，若出现「规则无法直接定论」的场景则标记冲突，交给 LLM

设计要点：
    规则命中 fail 时**不直接拒绝用户**，而是标记 conflict_detected，
    把「政策允许但订单超期」这类灰区交给 llm_reasoning_node 综合判断 —— 
    这是「规则引擎 + LLM」混合决策的核心边界。

输入：intent / facts / order_facts / rewritten_input
输出：policy_clauses / policy_basis / rule_result / decision /
      conflict_detected / conflict_type / need_execute / execute_payload
"""
from app.tool.policy_retrieval_tool import search_clauses, format_clauses
from app.agent.node.after_sales.schema import (
    RULES,
    combine_decisions,
    freight_note,
    INTENT_LABELS,
)

# 需要触发执行动作的决策
EXECUTE_ACTIONS = {
    "approve_return": "create_ticket",
    "approve_exchange": "create_ticket",
    "approve_repair": "create_ticket",
    "notify_existing": "notify",
    "reject_or_convert": None,
}

# 决策 → 对用户的话术前缀
ACTION_TEXT = {
    "approve_return": "可以为您办理退货",
    "approve_exchange": "可以为您办理换货",
    "approve_repair": "可以为您安排维修",
    "reject": "这笔订单暂时不支持该售后申请",
    "reject_or_convert": "这笔订单无法直接办理退货",
    "notify_existing": "这笔订单已经有一笔售后申请在处理中",
    "consult": "需要进一步确认",
}


def _build_query(state: dict) -> str:
    """拼一个尽量贴近政策语料的检索词。"""
    intent = state.get("intent", "")
    facts = state.get("facts") or {}
    label = INTENT_LABELS.get(intent, "售后")
    parts = [label, state.get("rewritten_input") or state.get("user_input", "")]
    if facts.get("reason"):
        parts.append(str(facts["reason"]))
    if facts.get("damage_desc"):
        parts.append(str(facts["damage_desc"]))
    return " ".join(p for p in parts if p)


def _existing_after_sales_state(state: dict) -> dict | None:
    """
    「已有进行中的售后单」是确定性结论，直接短路，不走 LLM 灰区推理。

    为什么：
        这不是政策灰区，而是硬业务约束 —— 同一订单不允许重复提交售后。
        让 LLM 去"裁决"只会引入不确定性（实测会把结论改成 consult 转人工，
        甚至反过来追问用户订单号），既浪费一次模型调用又答非所问。
    """
    order_facts = state.get("order_facts") or {}
    if not order_facts.get("has_existing_after_sales"):
        return None

    existing = order_facts.get("existing_after_sales") or {}
    reason_text = existing.get("description") or existing.get("type_text") or ""
    basis = ["POLICY-01"]

    decision = {
        "action": "notify_existing",
        "reason_code": "EXISTING_AFTER_SALES",
        "refund_amount": 0.0,
        "freight_compensation": 0.0,
        "resolved_by": "rule_shortcut",
        "existing_type": existing.get("type_text", ""),
        "existing_reason": reason_text,
        "notes": [
            f"该订单已存在售后单（类型：{existing.get('type_text') or '未知'}"
            f"{'，描述：' + reason_text if reason_text else ''}）"
        ],
    }
    payload = {
        "tool": "notify",
        "action": "notify_existing",
        "order_id": str(order_facts.get("order_id") or ""),
        "user_id": str(state.get("user_id") or ""),
        "reason_code": "EXISTING_AFTER_SALES",
    }

    print(
        f"[policy_rules] 已有售后单短路：type={existing.get('type_text')} "
        f"desc={reason_text} → notify_existing"
    )

    return {
        "policy_clauses": [],
        "policy_text": "（该订单已有进行中的售后单，无需重新检索政策）",
        "policy_basis": basis,
        "rule_result": {"results": [], "summary": decision},
        "decision": decision,
        "conflict_detected": False,
        "conflict_type": "existing_after_sales",
        "need_execute": True,
        "execute_payload": payload,
    }


def policy_rules_node(state: dict) -> dict:
    order_facts = state.get("order_facts") or {}

    shortcut = _existing_after_sales_state(state)
    if shortcut is not None:
        return shortcut

    # ---------- ① 政策检索 ----------
    try:
        clauses = search_clauses(_build_query(state), top_k=3)
    except Exception as e:
        print(f"[policy_rules] 政策检索失败(已忽略): {e}")
        clauses = []
    policy_text = format_clauses(clauses)
    print(f"[policy_rules] 命中政策条款: {[c['clause_id'] for c in clauses]}")

    # ---------- ② 规则判定 ----------
    results = []
    for rule in RULES:
        try:
            if not rule["cond"](state):
                results.append({"id": rule["id"], "name": rule["name"], "skipped": True})
                continue
            passed = bool(rule["check"](state))
            outcome = rule["pass"] if passed else rule["fail"]
            results.append({
                "id": rule["id"],
                "name": rule["name"],
                "skipped": False,
                "decision": "pass" if passed else "fail",
                "result": outcome or {},
                # 信息型规则 fail 时仍要拿到 pass 的业务字段（如运费补偿额度）
                "pass_result": rule.get("pass") or {},
                "informational": bool(rule.get("informational")),
                # 只有硬规则的 fail 才算冲突，信息型规则不触发 LLM 复核
                "conflict": rule.get("conflict") if (not passed and not rule.get("informational")) else None,
                "basis": rule.get("basis") or [],
            })
        except Exception as e:
            print(f"[policy_rules] 规则 {rule['id']} 执行异常(已跳过): {e}")

    # ---------- ③ 合并结果 + 冲突检测 ----------
    decision = combine_decisions(results, order_facts)

    # 运费承担说明：这才是「有无运费险」的正确落点 —— 影响谁出运费，不影响能否退货
    decision["freight_note"] = freight_note(state, decision)
    if decision["freight_note"]:
        decision.setdefault("notes", []).append(decision["freight_note"])

    conflict_type = ""
    for r in results:
        if r.get("decision") == "fail" and r.get("conflict"):
            conflict_type = r["conflict"]
            break

    # 动作本身表示「无法直接办理」时也交给 LLM 复核：
    #   - reject_or_convert / consult：本来就是灰区
    #   - reject：硬性拒绝前也要让 LLM 找替代方案（换货/维修/人工），
    #     避免系统生硬地对用户说"不能办"
    action = decision.get("action", "")
    if action in ("reject_or_convert", "consult"):
        conflict_detected = True
    elif action == "reject":
        conflict_detected = True
        conflict_type = conflict_type or "other"
    else:
        conflict_detected = bool(conflict_type)

    # 政策依据：规则命中的条款 + 检索到的条款（去重保序）
    basis: list[str] = []
    for r in results:
        for b in r.get("basis") or []:
            if b not in basis:
                basis.append(b)
    for c in clauses:
        if c["clause_id"] not in basis:
            basis.append(c["clause_id"])

    # ---------- ④ 需要执行的场景 ----------
    action = decision.get("action", "consult")
    tool = EXECUTE_ACTIONS.get(action)
    need_execute = bool(tool)
    execute_payload = {}
    if need_execute:
        facts = state.get("facts") or {}
        execute_payload = {
            "tool": tool,
            "order_id": str(facts.get("order_id") or order_facts.get("order_id") or ""),
            "action": action,
            "refund_amount": decision.get("refund_amount", 0.0),
            "freight_compensation": decision.get("freight_compensation", 0.0),
            "reason_code": decision.get("reason_code", ""),
            "clause_ids": basis[:3],
        }

    print(
        f"[policy_rules] action={action} conflict={conflict_detected}"
        f"({conflict_type}) need_execute={need_execute}"
    )

    return {
        "policy_clauses": clauses,
        "policy_text": policy_text,
        "policy_basis": basis[:4],
        "rule_result": {"results": results, "summary": decision},
        "decision": decision,
        "conflict_detected": conflict_detected,
        "conflict_type": conflict_type,
        "need_execute": need_execute,
        "execute_payload": execute_payload,
    }
