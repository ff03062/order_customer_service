"""
订单事实聚合工具（售后决策子图的「事实层」，纯函数，0 次 LLM 调用）

为什么单独抽一层，而不是复用订单查询子图？
    订单子图解决的是「我不知道用户要查什么」——需要 Text2SQL 的灵活性；
    售后决策解决的是「我明确知道要哪些字段，只是要去取」——字段集合固定。

    如果售后链路去挂订单子图，会引入 2~3 次多余的 LLM 调用
    （意图分类 → SQL 生成 → 结果翻译）以及「SQL 生成错了要重试」的不确定性，
    而售后决策的时效、金额、品类判断必须建立在精确数值上。

设计原则：
    1. 只读：底层走 run_readonly_query，继承「禁写操作 + 强制 user_id 过滤」
    2. 参数化：只接收 order_id / user_id，不做任何自然语言解析
    3. 列名自适应：先探测 information_schema 的真实列名再拼 SQL，
       避免因不同建表脚本（pay_amount / total_price / actual_amount）导致 Unknown column 1054
    4. 容错：任何一层失败都返回缺省结构，由规则层按「事实缺失」处理，不抛异常
"""
import json
from datetime import datetime

from app.tool.mysql_tool import run_readonly_query, to_datetime

# ===== 政策时效常量（与 data/policies/after_sales_policy.md 保持一致）=====
NO_REASON_RETURN_DAYS = 7  # 七天无理由退货
NO_REASON_EXCHANGE_DAYS = 15  # 十五天换货
WARRANTY_DAYS = 365  # 一年保修
FREIGHT_INSURANCE_MAX = 20.0  # 运费险最高补偿 20 元

# 不支持七天无理由退货的特殊品类
SPECIAL_CATEGORIES = ["生鲜", "食品", "定制", "贴身", "软件", "虚拟", "内衣"]

# orders.status：0待支付 1已支付 2已发货 3已取消 4售后中 5已完成
RECEIVED_STATUS = (2, 4, 5)  # 已发货 / 售后中 / 已完成 → 视为已签收

# ===== 候选列名（按优先级探测，取第一个真实存在的）=====
COLUMN_CANDIDATES = {
    "pay_amount": ["pay_amount", "actual_amount", "payment_amount", "paid_amount"],
    "total_amount": ["total_amount", "total_price", "amount"],
    "created_at": ["created_at", "create_time", "created_time", "order_time"],
}

# ===== 候选表名（不同建表脚本对明细表、分类表的命名不同）=====
TABLE_CANDIDATES = {
    "order_items": ["order_items", "order_item", "order_detail", "order_details", "order_goods"],
    "products": ["products", "product", "goods"],
    "categories": ["categories", "category", "product_categories"],
    "after_sales": ["after_sales", "after_sale", "after_sales_order", "refund"],
    "logistics": ["logistics", "logistics_info", "order_logistics", "shipping", "express"],
}

# 列名缓存：{table: set(columns)}，进程内只探测一次
_TABLE_COLUMNS: dict[str, set] = {}


def _parse_rows(raw):
    """把 run_readonly_query 的返回解析成 list[dict]，失败返回 None。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _table_columns(table: str) -> set:
    """探测表的真实列名（带进程内缓存），失败返回空集合。"""
    if table in _TABLE_COLUMNS:
        return _TABLE_COLUMNS[table]

    sql = (
            "SELECT COLUMN_NAME AS c /* probe */ FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '%s'" % table
    )
    rows = _parse_rows(run_readonly_query(sql))
    columns = {str(r.get("c")) for r in rows if r.get("c")} if rows else set()
    _TABLE_COLUMNS[table] = columns
    return columns


def _list_tables() -> set:
    """列出当前库的所有表名（带缓存），失败返回空集合。"""
    if "::tables" in _TABLE_COLUMNS:
        return _TABLE_COLUMNS["::tables"]

    sql = (
        "SELECT TABLE_NAME AS t /* tables */ FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE()"
    )
    rows = _parse_rows(run_readonly_query(sql)) or []
    tables = {str(r.get("t")) for r in rows if r.get("t")}
    _TABLE_COLUMNS["::tables"] = tables
    return tables


def _resolve_table(logical: str) -> str:
    """按候选表名找真实存在的表；都不存在则返回空串。"""
    tables = _list_tables()
    if not tables:
        return ""
    for name in TABLE_CANDIDATES.get(logical, [logical]):
        if name in tables:
            return name
    return ""


def _pick(candidates: list[str], available: set) -> str:
    """按优先级从候选列名中挑第一个真实存在的；都没有则返回空串。"""
    for name in candidates:
        if name in available:
            return name
    return ""


def _pick_fuzzy(candidates: list[str], available: set) -> str:
    """
    按优先级挑列名：精确匹配 → 列名被候选包含 → 候选被列名包含。

    三个层次的优先级不能颠倒，否则会出问题：
        候选 "status" 若直接做「候选 in 列名」匹配，会先撞上 "order_status"，
        导致拿错列。所以先精确、再「列名 ⊂ 候选」（更具体），
        最后才做「候选 ⊂ 列名」（更宽泛，且取最短的列名减少误配）。
    """
    exact = _pick(candidates, available)
    if exact:
        return exact

    lowered = {column: column.lower() for column in available}

    # 第二轮：列名是候选名的子串（例如候选 pay_amount_time 命中 pay_amount）
    for name in candidates:
        for column, low in sorted(lowered.items()):
            if low and low in name:
                return column

    # 第三轮：候选名是列名的子串（例如候选 insurance 命中 freight_insurance），取最短列名
    for name in candidates:
        hits = [column for column, low in lowered.items() if name in low]
        if hits:
            return sorted(hits, key=len)[0]

    return ""


def _filter_clause(order_id: int) -> str:
    """
    构造订单号过滤条件。

    兼容两种建表风格：
        - order_id 是 BIGINT（数字比较）
        - order_id 是 VARCHAR（字符串比较，需要加引号）
    """
    types = _columns_with_type("orders")
    if types.get("order_id", "").lower().startswith(("varchar", "char", "text")):
        return "order_id = '%s'" % order_id
    return "order_id = %s" % order_id


def _columns_with_type(table: str) -> dict:
    """探测列名 + 类型（用于判断 order_id 是数字还是字符串）。"""
    cache_key = f"{table}::types"
    if cache_key in _TABLE_COLUMNS:
        return _TABLE_COLUMNS[cache_key]

    sql = (
            "SELECT COLUMN_NAME AS c, DATA_TYPE AS t FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '%s'" % table
    )
    rows = _parse_rows(run_readonly_query(sql)) or []
    mapping = {str(r.get("c")): str(r.get("t")) for r in rows if r.get("c")}
    _TABLE_COLUMNS[cache_key] = mapping
    return mapping


def get_order_facts(order_id, user_id) -> dict:
    """
    聚合售后决策所需的订单事实。

    返回字典字段固定，供规则层与 LLM 层共同消费：
        {
          "found": bool, "order_id": str, "order_status": int,
          "pay_amount": float, "created_at": str,
          "days_since_purchase": int,
          "is_signed": bool, "signed_source": str,
          "days_since_receipt": int,        # 未签收时为 -1
          "categories": [str], "logistics_status": int, "has_freight_insurance": bool,
          "has_existing_after_sales": bool,
          "existing_after_sales": {"type","type_text","description","reason","status"},
          "error": str
        }
    关键业务语义：
        is_signed = False 表示商品还在路上 —— 此时用户随时可退，
        「七天无理由」的时效根本还没开始计算；只有 is_signed = True 才谈天数。
    """
    facts = {
        "found": False,
        "order_id": str(order_id or ""),
        "order_status": -1,
        "pay_amount": 0.0,
        "created_at": "",
        "days_since_purchase": -1,  # 下单距今天数（保修期判定用）
        "is_signed": False,  # 是否已签收（退货时效的第一道门）
        "signed_source": "",  # 签收判定的依据：logistics / orders.status
        "days_since_receipt": -1,  # 签收距今天数；未签收时为 -1
        "categories": [],
        "logistics_status": -1,  # 0未入库 1运输中 2已签收
        "has_freight_insurance": False,
        "has_existing_after_sales": False,
        "existing_after_sales": {},  # 已有售后单的 {type, type_text, description, reason, status}
        "error": "",
    }

    if not order_id:
        facts["error"] = "缺少订单号"
        return facts

    if not str(order_id).isdigit():
        facts["error"] = f"订单号格式非法：{order_id}"
        return facts

    oid = int(order_id)

    # ---------- 1. 订单主表（列名自适应，一次取回）----------
    columns = _table_columns("orders")
    if not columns:
        facts["error"] = "无法读取 orders 表结构（数据库不可用或无权限）"
        return facts

    selected = ["order_id"]
    for logical in ("pay_amount", "total_amount", "created_at"):
        picked = _pick_fuzzy(COLUMN_CANDIDATES[logical], columns)
        if picked and picked not in selected:
            selected.append(picked)
    status_col = _pick_fuzzy(["status", "order_status"], columns)
    if status_col and status_col not in selected:
        selected.append(status_col)

    select_sql = ", ".join(selected)
    order_sql = (
            "SELECT %s FROM orders WHERE %s AND user_id = {user_id} LIMIT 1"
            % (select_sql, _filter_clause(oid))
    )
    print(f"[order_facts] 订单查询列表名: {selected}")

    rows = _parse_rows(run_readonly_query(order_sql, user_id))
    if rows is None:
        facts["error"] = "订单查询失败（数据库不可用或语句非法）"
        return facts
    if not rows:
        facts["error"] = "订单不存在或不属于当前用户"
        return facts

    row = rows[0]

    def value_of(logical):
        """按候选列名从结果行里取值（与 SELECT 时用的是同一套列名）。"""
        picked = _pick_fuzzy(COLUMN_CANDIDATES[logical], columns)
        return row.get(picked) if picked else None

    facts["found"] = True
    facts["order_status"] = int(row.get(status_col) or 0) if status_col else -1

    def to_float(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    # 退款金额按实付算；pay_amount 取不到（或为 0）时退回订单总额
    facts["pay_amount"] = (
        to_float(value_of("pay_amount")) or to_float(value_of("total_amount"))
    )

    created_at = value_of("created_at")
    facts["created_at"] = str(created_at or "")
    purchase_dt = to_datetime(created_at)
    if purchase_dt:
        facts["days_since_purchase"] = max(0, (datetime.now() - purchase_dt).days)

    # ---------- 签收状态判定（退货时效的第一道门）----------
    #
    # 业务逻辑：
    #   未签收 → 商品还在路上，用户随时可退（走「未收货取消订单」流程）
    #   已签收 → 才开始计算七天无理由 / 十五天换货的时效
    #
    # 所以这里必须先确定「是否已签收」，再谈天数。
    # 物流状态比订单状态更权威：logistics.status = 2 即已签收。
    logistics_status = _query_logistics_status(oid, user_id)
    facts["logistics_status"] = logistics_status

    receipt_dt = _query_receipt_time(oid, user_id)
    if logistics_status == 2:
        is_signed, signed_source = True, "logistics"
    elif logistics_status in (0, 1):
        # 物流表明确显示未入库/运输中 → 未签收
        is_signed, signed_source = False, "logistics"
    else:
        # 没有物流记录：退回订单状态判断（2已发货/4售后中/5已完成 → 视为已收货）
        is_signed = facts["order_status"] in RECEIVED_STATUS
        signed_source = "orders.status"

    facts["is_signed"] = is_signed
    facts["signed_source"] = signed_source

    if receipt_dt is None and is_signed:
        # 已签收但物流没记录签收时间：用下单时间近似（结果是「偏长」，判定偏严）
        receipt_dt = purchase_dt

    if receipt_dt:
        facts["days_since_receipt"] = max(0, (datetime.now() - receipt_dt).days)
    # 未签收时 days_since_receipt 保持 -1：规则层据此走「未签收可退」分支

    # ---------- 2. 商品品类（表名/列名全部探测，取不到就跳过，不影响时效判定）----------
    facts["categories"] = _query_categories(oid, user_id)

    # ---------- 3. 运费险（按候选列名逐个探测，都不存在则保持 False）----------
    if columns:
        ins_col = _pick_fuzzy(
            ["freight_insurance", "shipping_insurance", "insurance"], columns
        )
        if ins_col:
            ins_sql = "SELECT %s AS ins /* ins */ FROM orders WHERE %s LIMIT 1" % (
                ins_col,
                _filter_clause(oid),
            )
            ins_rows = _parse_rows(run_readonly_query(ins_sql, user_id))
            if ins_rows:
                facts["has_freight_insurance"] = bool(ins_rows[0].get("ins"))

    # ---------- 4. 已有售后单：类型 + 描述直接读库（不追问用户）----------
    existing = _query_existing_after_sales(oid, user_id)
    facts["has_existing_after_sales"] = bool(existing)
    facts["existing_after_sales"] = existing

    return facts


# 售后类型枚举（after_sales.type）：1质量问题 2退货 3换货 4维修
AFTER_SALES_TYPE_TEXT = {
    1: "质量问题",
    2: "退货",
    3: "换货",
    4: "维修",
}


def _query_existing_after_sales(order_id: int, user_id) -> dict:
    """
    查询该订单已有的售后单，把「类型」和「描述」一并带出来。

    为什么这么做（而不是反问用户）：
        after_sales 表里本来就有 type（1质量问题/2退货/3换货/4维修）和
        description（文字描述）。订单号 + user_id 一关联就能确认，
        再去问用户"请问是什么原因"属于多余交互 ——
        数据里已经有的信息，就不该问用户。

    返回：{"id","type","type_text","description","reason","status"}；没有则 {}。
    """
    table = _resolve_table("after_sales")
    if not table:
        return {}

    columns = _table_columns(table)
    order_col = _pick_fuzzy(["order_id", "order_no"], columns)
    if not order_col:
        return {}

    id_col = _pick_fuzzy(["id"], columns)
    type_col = _pick_fuzzy(["type", "after_sales_type"], columns)
    desc_col = _pick_fuzzy(["description", "desc", "reason", "remark"], columns)
    status_col = _pick_fuzzy(["status"], columns)

    selected = []
    for col in (id_col, type_col, desc_col, status_col):
        if col and col not in selected:
            selected.append(col)
    if not selected:
        selected = [order_col]

    sql = "SELECT %s /* as_detail */ FROM %s WHERE %s = %s ORDER BY %s DESC LIMIT 1" % (
        ", ".join(selected),
        table,
        order_col,
        order_id,
        id_col or order_col,
    )
    rows = _parse_rows(run_readonly_query(sql, user_id)) or []
    if not rows:
        return {}

    row = rows[0]
    try:
        type_int = int(row.get(type_col)) if type_col else 0
    except (TypeError, ValueError):
        type_int = 0

    description = str(row.get(desc_col) or "").strip() if desc_col else ""
    type_text = AFTER_SALES_TYPE_TEXT.get(type_int, "")

    return {
        "id": row.get(id_col) if id_col else None,
        "type": type_int,
        "type_text": type_text,
        "description": description,
        # 对外统一的原因：优先文字描述，没有就用类型枚举翻译
        "reason": description or type_text,
        "status": row.get(status_col) if status_col else None,
    }


def _query_receipt_time(order_id: int, user_id):
    """
    从物流表取「签收时间」。

    返回 datetime | None：
        只有物流 status = 2（已签收）的记录才能作为签收时间；
        取不到就返回 None，由调用方按订单状态判断「是否已签收」。

    为什么不用「最新一条物流记录」兜底：
        揽收/运输记录的时间离下单很近（实测只差 8 分钟），
        拿它当签收时间会算出错误的天数，进而把本该可退的订单判成超期。
    """
    table = _resolve_table("logistics")
    if not table:
        return None

    columns = _table_columns(table)
    order_col = _pick_fuzzy(["order_id", "order_no"], columns)
    if not order_col:
        return None

    status_col = _pick_fuzzy(["status"], columns)
    time_col = _pick_fuzzy(
        ["sign_time", "signed_at", "receive_time", "receipt_time", "updated_at", "update_time"],
        columns,
    )
    if not time_col:
        return None

    if not status_col:
        return None

    sql = (
            "SELECT %s AS t /* signed */ FROM %s WHERE %s = %s AND %s = 2 "
            "ORDER BY %s DESC LIMIT 1" % (time_col, table, order_col, order_id, status_col, time_col)
    )
    rows = _parse_rows(run_readonly_query(sql, user_id))
    if rows:
        return to_datetime(rows[0].get("t"))
    return None


def _query_logistics_status(order_id: int, user_id):
    """
    查询物流状态（0未入库 1运输中 2已签收）；查不到返回 -1。

    物流状态是判断「是否已签收」的第一依据，比订单状态更准确。
    """
    table = _resolve_table("logistics")
    if not table:
        return -1

    columns = _table_columns(table)
    order_col = _pick_fuzzy(["order_id", "order_no"], columns)
    status_col = _pick_fuzzy(["status"], columns)
    if not order_col or not status_col:
        return -1

    sql = "SELECT %s AS s /* lg */ FROM %s WHERE %s = %s LIMIT 1" % (
        status_col,
        table,
        order_col,
        order_id,
    )
    rows = _parse_rows(run_readonly_query(sql, user_id))
    if rows and rows[0].get("s") is not None:
        try:
            return int(rows[0]["s"])
        except (TypeError, ValueError):
            return -1
    return -1


def _query_categories(order_id: int, user_id) -> list[str]:
    """
    查询订单商品所属品类。

    表名与列名全部靠探测（不同建表脚本差异很大），
    任何一步缺失都直接返回空列表 —— 品类只是「特殊品类判定」的输入，
    拿不到时按普通品类处理，不能因此让整条售后链路失败。
    """
    items_table = _resolve_table("order_items")
    if not items_table:
        return []

    items_columns = _table_columns(items_table)
    items_order_col = _pick_fuzzy(["order_id", "order_no"], items_columns)
    product_col = _pick_fuzzy(["product_id", "goods_id", "item_id"], items_columns)
    if not items_order_col or not product_col:
        return []

    products_table = _resolve_table("products")
    if not products_table:
        return []

    product_columns = _table_columns(products_table)
    product_id_col = _pick_fuzzy(["id", "product_id", "goods_id"], product_columns)
    if not product_id_col:
        return []

    category_col = _pick_fuzzy(["category_id", "cate_id", "cat_id"], product_columns)
    categories_table = _resolve_table("categories")

    if category_col and categories_table:
        categories_columns = _table_columns(categories_table)
        cat_id_col = _pick_fuzzy(["id", "category_id", "cate_id"], categories_columns)
        name_col = _pick_fuzzy(["name", "category_name", "title"], categories_columns)
        if cat_id_col and name_col:
            sql = (
                    "SELECT c.%s AS category_name /* cat */ "
                    "FROM %s i "
                    "LEFT JOIN %s p ON p.%s = i.%s "
                    "LEFT JOIN %s c ON c.%s = p.%s "
                    "WHERE i.%s = %s LIMIT 5"
                    % (
                        name_col,
                        items_table,
                        products_table,
                        product_id_col,
                        product_col,
                        categories_table,
                        cat_id_col,
                        category_col,
                        items_order_col,
                        order_id,
                    )
            )
            rows = _parse_rows(run_readonly_query(sql, user_id)) or []
            names = []
            for r in rows:
                name = r.get("category_name")
                if name and str(name) not in names:
                    names.append(str(name))
            return names

    # 没有分类表时，退化用商品名做关键词判定（仍可能命中"生鲜""定制"等词）
    sql = (
            "SELECT p.%s AS product_name /* pname */ "
            "FROM %s i "
            "LEFT JOIN %s p ON p.%s = i.%s "
            "WHERE i.%s = %s LIMIT 5"
            % (
                _pick_fuzzy(["name", "product_name", "title"], product_columns) or product_id_col,
                items_table,
                products_table,
                product_id_col,
                product_col,
                items_order_col,
                order_id,
            )
    )
    rows = _parse_rows(run_readonly_query(sql, user_id)) or []
    return [str(r.get("product_name")) for r in rows if r.get("product_name")]


def is_special_category(categories) -> bool:
    """判断商品是否属于不支持七天无理由退货的特殊品类。"""
    if not categories:
        return False
    joined = " ".join(str(c) for c in categories)
    return any(kw in joined for kw in SPECIAL_CATEGORIES)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    print("=== 表探测结果 ===")
    for logical in TABLE_CANDIDATES:
        print(f"  {logical:12s} → {_resolve_table(logical) or '（未找到）'}")
    print("orders 表列名：", sorted(_table_columns("orders")))
    print()
    print("=== 订单 1 的事实 ===")
    print(json.dumps(get_order_facts(1, 1), ensure_ascii=False, indent=2))
