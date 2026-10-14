"""
售后决策子图的业务 Schema 配置。

包含四块：
    1. INTENT_OPTIONS / INTENT_LABELS   二级售后意图枚举
    2. REQUIRED_SLOTS / SLOT_QUESTIONS  槽位定义与反问话术
    3. ORDER_STATUS_MAP / AFTER_SALES_MAP 状态码映射（供 LLM 理解业务语义）
    4. RULES                            规则表（确定性判定，程序执行）
"""
from app.tool.order_facts_tool import (
    NO_REASON_RETURN_DAYS,
    NO_REASON_EXCHANGE_DAYS,
    WARRANTY_DAYS,
    FREIGHT_INSURANCE_MAX,
    is_special_category,
)

# ============================================================
# 一、二级售后意图
# ============================================================

INTENT_OPTIONS = [
    "return_refund_apply",      # 申请退货退款
    "refund_status",            # 查询退款进度
    "no_reason_return",         # 七天无理由退货
    "refund_amount_dispute",    # 退款金额争议
    "exchange_apply",           # 申请换货
    "repair_apply",             # 申请维修
    "warranty_claim",           # 保修申请
    "freight_insurance",        # 运费险咨询 / 理赔
    "freight_dispute",          # 运费承担争议
    "price_protection",         # 价保申请
    "complaint",                # 投诉
    "logistics_damage",         # 物流损坏 / 丢件
    "special_goods_policy",     # 特殊商品政策咨询
]

INTENT_LABELS = {
    "return_refund_apply": "申请退货退款",
    "refund_status": "查询退款进度",
    "no_reason_return": "七天无理由退货",
    "refund_amount_dispute": "退款金额争议",
    "exchange_apply": "申请换货",
    "repair_apply": "申请维修",
    "warranty_claim": "保修申请",
    "freight_insurance": "运费险咨询或理赔",
    "freight_dispute": "运费承担争议",
    "price_protection": "价保申请",
    "complaint": "投诉",
    "logistics_damage": "物流损坏或丢件",
    "special_goods_policy": "特殊商品政策咨询",
}

# ============================================================
# 二、槽位定义
# ============================================================

# 每个意图的必需槽位（order_id 缺失时直接从上下文/对话中取）
#
# 注意 REQUIRED_SLOTS 只描述「用户需要提供什么」；
# 如果数据库里已有售后单（after_sales.description / type），
# 原因会被 apply_existing_after_sales() 自动补全，不会追问用户。
REQUIRED_SLOTS = {
    "return_refund_apply": ["order_id", "reason"],
    "refund_status": ["order_id"],
    "no_reason_return": ["order_id"],
    "refund_amount_dispute": ["order_id"],
    "exchange_apply": ["order_id", "reason"],
    "repair_apply": ["order_id", "damage_desc"],
    "warranty_claim": ["order_id", "damage_desc"],
    "freight_insurance": ["order_id"],
    "freight_dispute": ["order_id"],
    "price_protection": ["order_id"],
    "complaint": ["order_id"],
    "logistics_damage": ["order_id", "damage_desc"],
    "special_goods_policy": [],          # 纯政策咨询，不需要订单
}

# 槽位缺失时的反问话术
SLOT_QUESTIONS = {
    "order_id": "请问您说的是哪一笔订单？把订单号发给我，我帮您查一下～",
    "reason": "方便说一下退货原因吗？（例如：不想要了 / 尺码不合适 / 有质量问题）",
    "damage_desc": "麻烦描述一下具体的问题情况（比如哪里损坏、什么故障），我好帮您判断走哪种售后～",
    "unboxed": "请问商品已经拆封使用了吗？这会影响可选的售后方式哦～",
}

# 槽位中文名（用于生成话术与提示词）
SLOT_LABELS = {
    "order_id": "订单号",
    "reason": "退货原因",
    "damage_desc": "问题描述",
    "unboxed": "是否拆封",
    "quality_issue": "是否存在质量问题",
    "invoice": "是否保留发票",
}

# 纯政策咨询类意图：不需要订单号，也不应该因为没有订单号而反问
POLICY_ONLY_INTENTS = {"special_goods_policy"}

# 从用户口述中抽取的事实槽位（数据库查不到的，必须问用户）
USER_FACT_SLOTS = ["reason", "damage_desc", "quality_issue", "unboxed", "invoice"]

# ============================================================
# 三、状态码映射（供 LLM 理解业务语义）
# ============================================================

ORDER_STATUS_MAP = "0待支付 1已支付 2已发货 3已取消 4售后中 5已完成"
AFTER_SALES_TYPE_MAP = "1质量问题 2退货 3换货 4维修"
AFTER_SALES_STATUS_MAP = "0待处理 1处理中 2已完成 3已拒绝"
LOGISTICS_STATUS_MAP = "0未入库 1运输中 2已签收"

# ============================================================
# 四、冲突类型
# ============================================================

CONFLICT_TYPES = {
    "unboxed_but_quality": "已拆封但存在质量问题（包装完整性与质量责任的权衡）",
    "expired_no_reason": "政策可退但订单已超出无理由退货期限",
    "special_category": "商品属于不支持无理由退货的特殊品类",
    "existing_after_sales": "该订单已有进行中的售后单",
    "logistics_damage_dispute": "物流损坏，责任归属待判定",
    "refund_amount_mismatch": "退款金额与用户预期不一致",
    "other": "其他需要综合判断的灰区场景",
}

# ============================================================
# 五、规则表（确定性判定）
# ============================================================
#
# 设计原则：
#   - 规则只做「确定的判定」：时效、品类、金额、既有售后单
#   - 命中 fail 时**不直接拒绝用户**，而是返回 conflict 交给 LLM 推理灰区
#   - cond 返回 False 表示该规则不适用（跳过），返回 True 才执行 check
#
# 每条规则结构：
#   {"id", "name", "cond"(state)->bool, "check"(state)->bool,
#    "pass"/"fail": {业务字段}, "conflict": 冲突类型, "basis": [条款编号],
#    "informational": bool}   # True = fail 只是信息，不构成拒绝理由}


def _intent(state):
    return state.get("intent", "")


def _facts(state):
    return state.get("order_facts") or {}


def _slots(state):
    return state.get("facts") or {}


def _is_return_like(state):
    return _intent(state) in (
        "no_reason_return", "return_refund_apply", "exchange_apply",
    )


def _signed(state):
    """订单是否已签收（签收状态是退货时效的第一道门）。"""
    return bool(_facts(state).get("is_signed"))


def _not_signed(state):
    """是否「未签收」—— 商品还在路上。"""
    return not _facts(state).get("is_signed")


def _receipt_days(state):
    """签收距今天数；未签收或无记录返回 -1。"""
    return _facts(state).get("days_since_receipt", -1)


def _within_receipt(limit: int):
    """
    生成「自签收起 N 天内」的判定。

    注意：这条判定**只在已签收时才有意义**。
    调用方的 cond 必须先用 _signed(state) 把未签收的情况挡掉，
    因为未签收期间「七天无理由」的时效还没开始计算（走 R000）。
    """
    def _check(state):
        days = _receipt_days(state)
        if days < 0:
            return True   # 已签收但拿不到天数 → 不拦截，交给 LLM 复核
        return days <= limit
    return _check


def _within_purchase(limit: int):
    """生成「自购买/下单起 N 天内」的判定（保修期用）。"""
    def _check(state):
        days = _facts(state).get("days_since_purchase", -1)
        if days < 0:
            return True
        return days <= limit
    return _check


RULES = [
    {
        "id": "R000",
        "name": "未签收期间可退（商品在途中，七天时效尚未开始）",
        # 只要未签收，不论下单多久都成立
        "cond": lambda s: _is_return_like(s) and _not_signed(s),
        "check": lambda s: True,
        "pass": {
            "action": "approve_return",
            "reason_code": "NOT_SIGNED",
            "refund_amount": None,
            "note": "商品尚未签收，处于可退货窗口内；无需适用七天无理由时效",
        },
        "fail": {},
        "conflict": None,
        "basis": ["POLICY-01"],
    },
    {
        "id": "R001",
        "name": f"七天无理由退货时效（自签收起 {NO_REASON_RETURN_DAYS} 天）",
        # 只有已签收才计算时效；未签收由 R000 处理。
        # 覆盖「无理由退货」和「泛化的退货申请」——用户只说"我要退货"时等同无理由。
        # 质量问题由 R004 短路，不会因为超期被判不可退。
        "cond": lambda s: _intent(s) in ("no_reason_return", "return_refund_apply") and _signed(s),
        "check": _within_receipt(NO_REASON_RETURN_DAYS),
        "pass": {"action": "approve_return", "refund_amount": None, "freight_compensation": 0.0},
        "fail": {"action": "reject_or_convert", "reason_code": "EXPIRED_NO_REASON"},
        "conflict": "expired_no_reason",
        "basis": ["POLICY-01"],
    },
    {
        "id": "R002",
        "name": f"十五天换货时效（自签收起 {NO_REASON_EXCHANGE_DAYS} 天）",
        "cond": lambda s: _intent(s) == "exchange_apply" and _signed(s),
        "check": _within_receipt(NO_REASON_EXCHANGE_DAYS),
        "pass": {"action": "approve_exchange", "freight_compensation": 0.0},
        "fail": {"action": "reject_or_convert", "reason_code": "EXPIRED_EXCHANGE"},
        "conflict": "expired_no_reason",
        "basis": ["POLICY-01"],
    },
    {
        "id": "R003",
        "name": "特殊品类不支持七天无理由退货",
        "cond": lambda s: _intent(s) in ("no_reason_return", "return_refund_apply"),
        "check": lambda s: not is_special_category(_facts(s).get("categories") or []),
        "pass": {"special_category_ok": True},
        "fail": {"action": "reject", "reason_code": "SPECIAL_CATEGORY"},
        "conflict": "special_category",
        "basis": ["POLICY-07"],
    },
    {
        "id": "R004",
        "name": "质量问题不受无理由时效限制",
        "cond": lambda s: bool(_slots(s).get("quality_issue")),
        "check": lambda s: True,
        "pass": {
            "action": "approve_return",
            "reason_code": "QUALITY_ISSUE",
            "note": "质量问题优先于包装完整性与无理由时效",
        },
        "fail": {},
        "conflict": None,
        "basis": ["POLICY-05", "POLICY-01"],
    },
    {
        "id": "R005",
        "name": f"保修期判定（自购买起 {WARRANTY_DAYS} 天）",
        # 保修期自「购买」起算，不是自签收起算，所以用 days_since_purchase
        "cond": lambda s: _intent(s) in ("warranty_claim", "repair_apply"),
        "check": _within_purchase(WARRANTY_DAYS),
        "pass": {"action": "approve_repair", "reason_code": "IN_WARRANTY"},
        "fail": {"action": "reject_or_convert", "reason_code": "OUT_OF_WARRANTY"},
        "conflict": "other",
        "basis": ["POLICY-05"],
    },
    {
        "id": "R006",
        "name": "运费险补偿判定（仅影响运费承担方，不影响能否退货）",
        # 只在用户明确问运费险/运费争议时，才把「无运费险」当成需要复核的冲突。
        # 普通退货申请不因缺少运费险而被拒绝 —— 运费险只决定退货运费谁出。
        "cond": lambda s: _intent(s) in ("freight_insurance", "freight_dispute"),
        "check": lambda s: bool(_facts(s).get("has_freight_insurance")),
        "pass": {"freight_compensation": FREIGHT_INSURANCE_MAX, "reason_code": "INSURANCE_OK"},
        "fail": {"freight_compensation": 0.0, "reason_code": "NO_INSURANCE"},
        # informational=True：这条规则的 fail 只是「信息」，不是「拦截」，
        # 绝不能被 combine_decisions 当成拒绝理由。
        "informational": True,
        "conflict": None,
        "basis": ["POLICY-04"],
    },
    {
        "id": "R007",
        "name": "退款金额计算",
        "cond": lambda s: _intent(s) in (
            "return_refund_apply", "no_reason_return", "refund_amount_dispute", "refund_status",
        ),
        "check": lambda s: True,
        "pass": {"refund_amount": None},   # 具体金额由 _apply_default_amount 用订单实付填充
        "fail": {},
        "conflict": None,
        "basis": ["POLICY-03"],
    },
    {
        "id": "R008",
        "name": "重复售后单拦截",
        "cond": lambda s: bool(_facts(s).get("has_existing_after_sales")),
        "check": lambda s: False,
        "pass": {},
        "fail": {"action": "notify_existing", "reason_code": "EXISTING_AFTER_SALES"},
        "conflict": "existing_after_sales",
        "basis": ["POLICY-01"],
    },
    {
        "id": "R009",
        "name": "订单状态可售后判定",
        # 只在真的取到订单事实时才判定（纯政策咨询没有订单，不应被这条规则拦下）
        "cond": lambda s: bool(_facts(s).get("found")),
        "check": lambda s: _facts(s).get("order_status") in (1, 2, 4, 5),
        "pass": {"order_status_ok": True},
        "fail": {"action": "reject", "reason_code": "ORDER_STATUS_INVALID"},
        "conflict": "other",
        "basis": ["POLICY-01"],
    },
]


def combine_decisions(results: list[dict], order_facts: dict) -> dict:
    """
    把多条规则结果合并成一个决策。

    关键区分（踩过坑）：
        - **硬规则**（默认）：fail 表示"不满足条件"，其 action 会参与最终决策，
          例如时效超期、特殊品类、订单状态不可售后。
        - **信息型规则**（informational=True）：fail 只是"补充说明"，
          例如"没有运费险"——它只影响运费谁承担，**绝不能构成拒绝退货的理由**。

    合并策略（保守优先）：
        1. 只有硬规则 fail 才提升 action 的严重度
        2. 信息型规则无论通过与否，都用 pass 里的业务字段，并把 fail 的原因写进 notes
        3. 金额：未指定时用订单实付金额；运费补偿取最大值
    """
    merged: dict = {
        "action": "consult",
        "refund_amount": None,
        "freight_compensation": 0.0,
        "reason_code": "RULE_PASS",
        "notes": [],
    }

    priority = {
        "reject": 4, "reject_or_convert": 3, "notify_existing": 3,
        "consult": 2, "approve_repair": 1, "approve_exchange": 1, "approve_return": 1,
    }

    worst = None
    refund: float | None = None
    freight = 0.0
    info_reason_code = ""
    notes: list[str] = []

    for r in results:
        if r.get("skipped"):
            continue

        passed = r.get("decision") == "pass"
        payload = r.get("result") or {}
        informational = bool(r.get("informational"))

        if not passed:
            if informational:
                # 信息型：用 pass 的业务字段，只把"为什么没享受到"记进 notes
                notes.append(f"{r['id']}：{r['name']}未满足（{payload.get('reason_code') or '不适用'}）")
                payload = r.get("pass_result") or {}
            else:
                action = payload.get("action", "reject")
                if worst is None or priority.get(action, 0) > priority.get(worst, 0):
                    worst = action
                notes.append(f"{r['id']}未通过：{r['name']}")
                if payload.get("reason_code"):
                    merged["reason_code"] = payload["reason_code"]

        for key in ("refund_amount", "freight_compensation", "note"):
            value = payload.get(key)
            if value is None:
                continue
            if key == "freight_compensation":
                freight = max(freight, float(value or 0))
            elif key == "note":
                notes.append(str(value))
            elif key == "refund_amount":
                refund = float(value)

    merged["refund_amount"] = refund
    merged["freight_compensation"] = freight
    merged["notes"] = notes

    if worst:
        merged["action"] = worst

    # 金额兜底：退货类且规则未指定金额时，用订单实付金额
    if merged.get("refund_amount") in (None, 0, 0.0) and merged["action"] in (
        "approve_return", "reject_or_convert",
    ):
        merged["refund_amount"] = float(order_facts.get("pay_amount") or 0)

    return merged


def freight_note(state: dict, decision: dict) -> str:
    """
    生成「运费由谁承担」的说明。

    业务规则：
        - 质量问题       → 运费由平台承担
        - 换货           → 往返运费由用户承担（运费险不适用换货）
        - 有运费险的退货 → 最高补偿 FREIGHT_INSURANCE_MAX 元
        - 无运费险的退货 → 用户自行承担

    这条说明只影响费用归属，**不影响能否退货**，所以绝不参与 action 判定。
    """
    action = decision.get("action", "")
    if action not in ("approve_return", "approve_exchange", "reject_or_convert"):
        return ""

    facts = state.get("facts") or {}
    order_facts = state.get("order_facts") or {}
    quality_issue = bool(facts.get("quality_issue"))
    has_insurance = bool(order_facts.get("has_freight_insurance"))
    compensation = float(decision.get("freight_compensation") or 0.0)

    if quality_issue:
        return "运费承担：属于质量问题，退换运费由平台承担"
    if action == "approve_exchange":
        return "运费承担：换货的往返运费需由您承担（运费险不适用于换货）"
    if has_insurance:
        return f"运费承担：本单有运费险，退货可获最高 {compensation:.0f} 元运费补偿"
    return "运费承担：本单没有运费险，退货运费需由您承担"


def missing_required_slots(intent: str, facts: dict) -> list[str]:
    """计算缺失的必需槽位（只依赖用户口述，不查库）。"""
    required = REQUIRED_SLOTS.get(intent, [])
    return [slot for slot in required if not facts.get(slot)]


def apply_existing_after_sales(intent: str, facts: dict, order_facts: dict) -> list[str]:
    """
    用数据库里已有的售后单补全槽位，并重新计算缺失项。

    为什么要有这一步：
        after_sales 表里本来就有 type（1质量问题/2退货/3换货/4维修）和
        description（文字描述）。订单号 + user_id 一关联就能确认原因，
        此时再问用户"请问是什么原因"就是多余交互 ——
        数据里能拿到的信息，不应该转成对用户的追问。

    返回：补全后仍然缺失的槽位列表。
    """
    existing = (order_facts or {}).get("existing_after_sales") or {}
    existing_reason = existing.get("reason") or ""

    if existing_reason and not facts.get("reason"):
        facts["reason"] = existing_reason
        facts["reason_source"] = "after_sales.type/description"

    return missing_required_slots(intent, facts)


def clarify_question_for(missing: list[str]) -> str:
    """按缺失槽位生成反问话术。"""
    if not missing:
        return "麻烦您再补充一点信息，我好帮您处理～"
    return SLOT_QUESTIONS.get(missing[0], "麻烦您再补充一点信息，我好帮您处理～")
