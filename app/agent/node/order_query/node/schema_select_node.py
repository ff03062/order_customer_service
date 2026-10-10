"""
节点 2: schema_select_node
职责：根据意图选择相关表/字段，渲染成精简 schema 文本。
输入：intent
输出：relevant_tables, relevant_fields, schema_text
"""
from app.agent.node.order_query.schema import INTENT_TO_SCHEMA, render_schema_for_intent


def schema_select_node(state: dict) -> dict:
    intent = state.get("intent", "query_orders")
    mapping = INTENT_TO_SCHEMA.get(intent, {})
    schema_text = render_schema_for_intent(intent)

    return {
        "relevant_tables": list(mapping.keys()),
        "relevant_fields": mapping,
        "schema_text": schema_text,
    }
