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
        """首次使用时幂等建表。"""
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

    async def query(self):
        await self._ensure_schema()
        async with pool.connection() as con:
            async with con.cursor() as cur:
                await cur.execute(
                    "SELECT summary FROM customer_conversation_summary WHERE session_id = %s",
                    (self.session_id,),
                )
                rs = await cur.fetchone()
                return rs[0] if rs else ""

    async def clear(self):
        await self._ensure_schema()
        async with pool.connection() as con:
            async with con.cursor() as cur:
                await cur.execute(
                    "DELETE FROM customer_conversation_summary WHERE session_id = %s",
                    (self.session_id,),
                )
            await con.commit()
