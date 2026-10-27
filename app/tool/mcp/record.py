"""
支付宝交易记录（SQLite 存档）

为什么必须存：
    alipay.py 的 refund() 每次调用都会生成**新的退款请求号**。
    如果网络超时后重试，用新号就会再退一次 —— 变成重复退款事故。

    所以生成的商户订单号必须落库，重试时用 last_trade_no() 取回原来那个号（幂等）。

存了什么：
    trade_no        商户订单号（FRT=收运费 / RFD=退款）
    order_id        关联的业务订单号
    biz_type        PAY=收运费 / REFUND=退款
    amount          金额
    raw_result      支付宝返回的原文，排错用
"""
import sqlite3
import time
from pathlib import Path

# app/tool/mcp/record.py -> parents[3] = 项目根目录
DB_PATH = str(Path(__file__).resolve().parents[3] / "data" / "alipay_records.db")


def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS alipay_records (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_no   TEXT NOT NULL,
            order_id   TEXT NOT NULL,
            biz_type   TEXT NOT NULL,
            amount     REAL NOT NULL,
            raw_result TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    con.commit()
    return con


def add_record(trade_no: str, order_id: str, biz_type: str, amount: float, raw_result: str = "") -> None:
    """记一笔交易。biz_type 传 PAY 或 REFUND。"""
    con = _conn()
    try:
        con.execute(
            "INSERT INTO alipay_records(trade_no, order_id, biz_type, amount, raw_result, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (trade_no, str(order_id), biz_type, float(amount), raw_result, time.time()),
        )
        con.commit()
    finally:
        con.close()


def last_trade_no(order_id: str, biz_type: str = "PAY") -> str:
    """取该订单最近一笔交易号；没有则返回空串。重试前用它拿回幂等号。"""
    con = _conn()
    try:
        row = con.execute(
            "SELECT trade_no FROM alipay_records WHERE order_id = ? AND biz_type = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (str(order_id), biz_type),
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else ""
