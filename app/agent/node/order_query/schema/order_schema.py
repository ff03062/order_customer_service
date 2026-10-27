"""
订单查询子图的 Schema 配置：意图映射、字段注释、状态码。
供各节点复用。

注意：这里只保留**渲染实际用到**的那一份字段定义（`FIELD_HINTS`）。
原来还有一份 `TABLES_SCHEMA`（同样内容、不同写法），既没有任何地方引用，
注释还和 `FIELD_HINTS` 不一致（`categories.parent_id` 少了"0=顶级"），已删除。
"""

# ===== 意图选项 =====
INTENT_OPTIONS = [
    "query_orders",        # 订单列表/详情/统计
    "query_order_items",   # 订单明细/商品
    "query_logistics",     # 物流
    "query_after_sales",   # 售后
    "query_products",      # 商品/分类（字典表）
    "chitchat",           # 不需要查库
]

# ===== 共用的状态码映射（意图识别 & 答案翻译共用） =====
STATUS_MAP = (
    "orders.status：0待支付 1已支付 2已发货 3已取消 4售后中 5已完成\n"
    "logistics.status：0未入库 1运输中 2已签收\n"
    "after_sales.type：1质量问题 2退货 3换货 4维修\n"
    "after_sales.status：0待处理 1处理中 2已完成 3已拒绝"
)

# ===== 意图 → 相关表 → 需要的字段 =====
INTENT_TO_SCHEMA: dict[str, dict[str, list[str]]] = {
    "query_orders": {
        "orders": ["order_id", "status", "total_amount", "pay_amount", "created_at"],
        "order_items": ["product_name"],
        "products": ["name", "category_id"],
        "categories": ["name"],

    },
    "query_order_items": {
        "orders": ["order_id", "status"],
        "order_items": ["product_name", "price", "quantity", "subtotal"],
    },
    "query_logistics": {
        "orders": ["order_id"],
        "logistics": ["company", "tracking_no", "status", "updated_at"],
    },
    "query_after_sales": {
        "after_sales": ["id", "type", "description", "status", "created_at"],
    },
    "query_products": {
        "products": ["name", "price", "stock"],
        "categories": ["name"],
    },
    "chitchat": {},
}

# ===== 字段注释：(table, field) → "类型  中文说明" =====
FIELD_HINTS: dict[tuple[str, str], str] = {
    ("orders", "order_id"): "BIGINT UNSIGNED  订单ID",
    ("orders", "user_id"): "BIGINT UNSIGNED  用户ID",
    ("orders", "status"): "TINYINT  订单状态：0待支付 1已支付 2已发货 3已取消 4售后中 5已完成",
    ("orders", "total_amount"): "DECIMAL(10,2)  订单总金额",
    ("orders", "pay_amount"): "DECIMAL(10,2)  实付金额",
    ("orders", "created_at"): "DATETIME  下单时间",
    ("order_items", "id"): "BIGINT UNSIGNED  明细ID",
    ("order_items", "order_id"): "BIGINT UNSIGNED  订单ID",
    ("order_items", "product_id"): "BIGINT UNSIGNED  商品ID",
    ("order_items", "product_name"): "VARCHAR(200)  下单时商品快照名",
    ("order_items", "price"): "DECIMAL(10,2)  下单时单价",
    ("order_items", "quantity"): "INT  购买数量",
    ("order_items", "subtotal"): "DECIMAL(10,2)  小计金额",
    ("logistics", "id"): "BIGINT UNSIGNED  物流ID",
    ("logistics", "order_id"): "BIGINT UNSIGNED  订单ID",
    ("logistics", "company"): "VARCHAR(50)  快递公司",
    ("logistics", "tracking_no"): "VARCHAR(64)  快递单号",
    ("logistics", "status"): "TINYINT  物流状态：0未入库 1运输中 2已签收",
    ("logistics", "updated_at"): "DATETIME  更新时间",
    ("after_sales", "id"): "BIGINT UNSIGNED  售后ID",
    ("after_sales", "order_id"): "BIGINT UNSIGNED  订单ID",
    ("after_sales", "user_id"): "BIGINT UNSIGNED  用户ID",
    ("after_sales", "type"): "TINYINT  售后类型：1质量问题 2退货 3换货 4维修",
    ("after_sales", "description"): "TEXT  问题描述",
    ("after_sales", "status"): "TINYINT  处理状态：0待处理 1处理中 2已完成 3已拒绝",
    ("after_sales", "created_at"): "DATETIME  创建时间",
    ("after_sales", "updated_at"): "DATETIME  更新时间",
    ("products", "id"): "BIGINT UNSIGNED  商品ID",
    ("products", "name"): "VARCHAR(200)  商品名称",
    ("products", "category_id"): "BIGINT UNSIGNED  分类ID",
    ("products", "price"): "DECIMAL(10,2)  现价",
    ("products", "stock"): "INT  库存",
    ("categories", "id"): "BIGINT UNSIGNED  分类ID",
    ("categories", "name"): "VARCHAR(50)  分类名称",
    ("categories", "parent_id"): "BIGINT UNSIGNED  父分类ID（0=顶级）",
}


# ===== 工具函数 =====

def _render_table_with_fields(table: str, fields: list[str]) -> str:
    """渲染单张表，只输出需要字段。"""
    lines = [f"# {table} 表"]
    for f in fields:
        hint = FIELD_HINTS.get((table, f), "")
        if hint:
            lines.append(f"- {f}  {hint}")
    return "\n".join(lines)


def render_schema_for_intent(intent: str) -> str:
    """根据意图返回裁剪过的 schema，只输出相关表 + 必要字段。"""
    tables_to_fields = INTENT_TO_SCHEMA.get(intent, {})
    if not tables_to_fields:
        return "（不需要查询数据库）"
    return "\n\n".join(
        _render_table_with_fields(table, fields)
        for table, fields in tables_to_fields.items()
    )
