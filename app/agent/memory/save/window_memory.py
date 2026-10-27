import redis.asyncio as redis
import json
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

"""
短期记忆 - 窗口记忆(电商客服场景)
存储用户与客服最近 N 轮对话
"""


class WindowMemory:
    def __init__(self, session_id):
        self.redis = redis.StrictRedis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            password=os.getenv("REDIS_PASSWORD"),
            db=int(os.getenv("REDIS_DB", 0)),
            socket_timeout=3,
            decode_responses=True,
        )
        # 窗口保留条数，读 .env 的 WINDOW_MEMORY_ROUNDS
        self.window_size = int(os.getenv("WINDOW_MEMORY_ROUNDS", 40))
        self.session_id = session_id
        self.key = f"customer_window:{self.session_id}"
        # 过期时间(秒)
        self.window_limit_time = int(os.getenv("WINDOW_MEMORY_TIME", 86400))

    async def save(self, role: str, content: str):
        data = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        msg = json.dumps(data, ensure_ascii=False)
        pipe = self.redis.pipeline()  # 创建一个管道，用于将多个命令打包在一起发送给Redis服务器
        pipe.rpush(self.key, msg)  # 将数据添加到列表的末尾
        pipe.ltrim(self.key, -self.window_size, -1)  # 只保留最近 window_size 条
        pipe.expire(self.key, self.window_limit_time)  # 设置过期时间
        await pipe.execute()

    async def query(self):
        if not await self.redis.exists(self.key):
            return []
        data = await self.redis.lrange(self.key, 0, -1)
        return [json.loads(i) for i in data]

    async def clear(self):
        await self.redis.delete(self.key)


if __name__ == "__main__":
    import asyncio


    async def main():
        w = WindowMemory("order_001")
        await w.save("user", "你好,我的订单还没发货")
        await w.save("ai", "您好,我帮您查一下订单状态")
        await w.save("user", "订单号是 202609280001")
        rs = await w.query()
        print(rs)


    asyncio.run(main())
