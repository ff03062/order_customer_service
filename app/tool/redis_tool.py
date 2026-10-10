import redis
import json
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


class WindowMemory:
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is None:
            cls._client = redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True
            )
        return cls._client

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.key = f"window_memory:{session_id}"
        self.window_size = int(os.getenv("WINDOW_MEMORY_ROUNDS", 40))
        self.ttl = int(os.getenv("WINDOW_MEMORY_TIME", 86400))

    def save(self, role: str, content: str):
        msg = json.dumps({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        }, ensure_ascii=False)

        pipe = self.get_client().pipeline()
        pipe.rpush(self.key, msg)                       # 新消息进尾部
        pipe.ltrim(self.key, -self.window_size, -1)     # 只保留最后 N 条
        pipe.expire(self.key, self.ttl)
        pipe.execute()

    def query(self):
        msgs = self.get_client().lrange(self.key, 0, -1)
        return [json.loads(m) for m in msgs]            # 天然旧→新，无需反转

    def clear(self):
        self.get_client().delete(self.key)