"""
节点 3: policy_rules_node
职责：政策检索 + 规则判定（确定性判定的落点）

流程：
    ① 政策检索（两跳：第一跳不够时由 LLM 判定并给出补充检索词）
    ② 遍历规则表，按 cond 判断适用性、按 check 判定通过与否
    ③ 合并规则结果，若出现「规则无法直接定论」的场景则标记冲突，交给 LLM

设计要点：
    规则命中 fail 时**不直接拒绝用户**，而是标记 conflict_detected，
    把「政策允许但订单超期」这类灰区交给 llm_reasoning_node 综合判断 —— 
    这是「规则引擎 + LLM」混合决策的核心边界。

输入：intent / facts / order_facts / rewritten_input
输出：policy_clauses / policy_basis / rule_result / decision /
      conflict_detected / conflict_type

注意：这个节点**不决定要不要执行、也不生成执行参数**。
     执行动作统一由 solution_generate_node 在最终决策确定之后算一次，
     两处各算一遍只会算出两个不一样的结论。
"""
from pydantic import BaseModel, Field

from app.agent.node.after_sales.common import structured_call
from app.tool.policy_retrieval_tool import search_clauses, format_clauses
from app.agent.node.after_sales.schema import (
    RULES,
    combine_decisions,
    freight_note,
    INTENT_LABELS,
)


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


class _HopJudge(BaseModel):
    """第二跳的判定结果。"""

    enough: bool = Field(description="第一跳条款是否已足够回答用户问题")
    next_query: str = Field(default="", description="不足时给出补充检索词，否则留空")


_HOP_PROMPT = """判断下面的售后政策检索结果是否够用。

【用户问题】{query}
【已命中条款】
{hit}

如果这些条款已经能回答用户问题，enough 为 true、next_query 留空。
如果不够，enough 为 false，并给出一个更具体的中文检索词 next_query（补充商品类型、售后场景等）。

只输出一个 json 对象：
{{"enough": true, "next_query": ""}}"""


def _retrieve_clauses(query: str, top_k: int = 3) -> list[dict]:
    """
    两跳检索。

    第一跳命中条数已满 top_k 就直接返回；否则让 LLM 判断覆盖度，
    不够就按它给的检索词再检一次，合并去重。
    """
    clauses = search_clauses(query, top_k=top_k)
    if len(clauses) >= top_k:
        return clauses

    hit = "\n".join(f"[{c['clause_id']}] {c['title']}" for c in clauses) or "（无）"
    judge = structured_call(_HopJudge, _HOP_PROMPT.format(query=query, hit=hit))
    if judge is None or judge.enough or not judge.next_query:
        return clauses

    extra = search_clauses(judge.next_query, top_k=top_k)
    print(f"[policy_rules] 第二跳 {judge.next_query!r} → {[c['clause_id'] for c in extra]}")
    seen = {c["clause_id"] for c in clauses}
    return (clauses + [c for c in extra if c["clause_id"] not in seen])[:top_k]


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
    }


def policy_rules_node(state: dict) -> dict:
    order_facts = state.get("order_facts") or {}

    shortcut = _existing_after_sales_state(state)
    if shortcut is not None:
        return shortcut

    # ---------- ① 政策检索 ----------
    clauses = _retrieve_clauses(_build_query(state), top_k=3)
    policy_text = format_clauses(clauses)
    print(f"[policy_rules] 命中政策条款: {[c['clause_id'] for c in clauses]}")

    # ---------- ② 规则判定 ----------
    results = []
    for rule in RULES:
        if not rule["cond"](state):
            results.append({"id": rule["id"], "name": rule["name"], "skipped": True})
            continue
        passed = bool(rule["check"](state))
        results.append({
            "id": rule["id"],
            "name": rule["name"],
            "skipped": False,
            "decision": "pass" if passed else "fail",
            "result": (rule["pass"] if passed else rule["fail"]) or {},
            # 信息型规则 fail 时仍要拿到 pass 的业务字段（如运费补偿额度）
            "pass_result": rule.get("pass") or {},
            "informational": bool(rule.get("informational")),
            # 只有硬规则的 fail 才算冲突，信息型规则不触发 LLM 复核
            "conflict": rule.get("conflict") if (not passed and not rule.get("informational")) else None,
            "basis": rule.get("basis") or [],
        })

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

    # ---------- ④ 冲突标记 ----------
    print(
        f"[policy_rules] action={action} conflict={conflict_detected}({conflict_type})"
    )

    return {
        "policy_clauses": clauses,
        "policy_text": policy_text,
        "policy_basis": basis[:4],
        "rule_result": {"results": results, "summary": decision},
        "decision": decision,
        "conflict_detected": conflict_detected,
        "conflict_type": conflict_type,
    }
