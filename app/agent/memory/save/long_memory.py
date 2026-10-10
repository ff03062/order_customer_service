import chromadb
import os
import uuid
import asyncio
from dotenv import load_dotenv

load_dotenv()


class LongMemory:
    """
    长期记忆 - ChromaDB 向量存储(电商客服场景)
    按用户存储:常买品类、退换货偏好、收货习惯等可被语义检索的偏好信息
    """

    def __init__(self):
        path = os.getenv("CHROMA_PATH", "./chroma_data")
        self.client = chromadb.PersistentClient(path)
        self.collection = self.client.get_or_create_collection("customer_long_memory")

    async def save(self, user_id, content: str):
        user_id = str(user_id)
        doc_id = f"customer_long_{user_id}_{uuid.uuid4()}"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self.collection.add(
                ids=[doc_id],
                documents=[content],
                metadatas=[{"user_id": user_id}],
            ),
        )

    async def query(self, user_id, question: str, top_k: int = 3):
        user_id = str(user_id)
        loop = asyncio.get_event_loop()
        rs = await loop.run_in_executor(
            None,
            lambda: self.collection.query(
                query_texts=[question],
                n_results=top_k,
                where={"user_id": user_id},
            ),
        )
        docs = rs.get("documents") or [[]]
        return docs[0] if docs else []


if __name__ == "__main__":
    async def main():
        m = LongMemory()
        await m.save("u_1001", "用户常用顺丰快递,偏好下午收货")
        await m.save("u_1001", "用户对尺码偏大,平时穿 L 码")
        rs = await m.query("u_1001", "用户穿什么尺码")
        print("查询结果:", rs)

    asyncio.run(main())
