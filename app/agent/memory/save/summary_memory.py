from psycopg_pool import AsyncConnectionPool
import os
from dotenv import load_dotenv

load_dotenv()

pool = AsyncConnectionPool(
    conninfo=os.getenv("POSTGRESQL_URL"),
    max_size=20,
    min_size=1,
    open=False,
    timeout=5,
)

class SummaryMemory:
    """
    摘要记忆 - PostgreSQL(电商客服场景)
    每个 session(用户会话)对应一行摘要,记录本轮对话的业务要点
    """

    _schema_ready = False

    def __init__(self, session_id: str):
        self.session_id = session_id

    async def _ensure_schema(self):
        """首次使用时幂等建表,避免 UndefinedTable 直接打断主流程。"""
        if SummaryMemory._schema_ready:
            return
        async with pool.connection() as con:
            async with con.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customer_conversation_summary (
                        session_id  VARCHAR(64) PRIMARY KEY,
                        summary     TEXT,
                        create_time TIMESTAMPTZ DEFAULT NOW(),
                        update_time TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
            await con.commit()
        SummaryMemory._schema_ready = True

    async def save(self, summary: str):
        try:
            await self._ensure_schema()
            async with pool.connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO customer_conversation_summary(session_id, summary)
                        VALUES(%s, %s)
                        ON CONFLICT(session_id)
                        DO UPDATE SET summary = EXCLUDED.summary, update_time = NOW()
                        """,
                        (self.session_id, summary),
                    )
                await con.commit()
        except Exception as e:
            print(f"[SummaryMemory.save] 写入失败(已忽略): {e}")

    async def query(self):
        try:
            await self._ensure_schema()
            async with pool.connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(
                        "SELECT summary FROM customer_conversation_summary WHERE session_id = %s",
                        (self.session_id,),
                    )
                    rs = await cur.fetchone()
                    return rs[0] if rs else ""
        except Exception as e:
            print(f"[SummaryMemory.query] 查询失败(已忽略): {e}")
            return ""

    async def clear(self):
        try:
            await self._ensure_schema()
            async with pool.connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM customer_conversation_summary WHERE session_id = %s",
                        (self.session_id,),
                    )
                await con.commit()
        except Exception as e:
            print(f"[SummaryMemory.clear] 删除失败(已忽略): {e}")
