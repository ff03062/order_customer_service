import redis.asyncio as redis
import os
from dotenv import load_dotenv

load_dotenv()


class ProfileMemory:
    """
    用户画像记忆 - Redis Hash(电商客服场景)
    存储用户结构化属性:姓名、收货地址、常用支付方式、会员等级等
    """

    def __init__(self, user_id):
        self.redis = redis.StrictRedis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB", 0)),
            socket_timeout=3,
            decode_responses=True,
        )
        self.user_id = str(user_id)
        self.key = f"customer_profile:{self.user_id}"

    async def save(self, hashkey: str, value):
        await self.redis.hset(self.key, hashkey, str(value))

    async def query(self):
        rs = await self.redis.hgetall(self.key)
        if not rs:
            return ""
        lines = []
        for k, v in rs.items():
            k = k.decode() if isinstance(k, bytes) else k
            v = v.decode() if isinstance(v, bytes) else v
            lines.append(f"{k}: {v}")
        return "\n".join(lines)

    async def clear(self):
        await self.redis.delete(self.key)


if __name__ == "__main__":
    import asyncio

    async def main():
        p = ProfileMemory("u_1001")
        await p.save("name", "张三")
        await p.save("vip_level", "黄金会员")
        await p.save("default_address", "上海市浦东新区张江路 100 号")
        print(await p.query())

    asyncio.run(main())
