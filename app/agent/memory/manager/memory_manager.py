from app.agent.memory.retrieval.SummaryAgent import SummaryAgent
from app.agent.memory.manager.session_mananger import SessionManager
from app.agent.memory.retrieval.LongAgent import LongAgent
from app.agent.memory.retrieval.profile_agent import ProfileAgent

"""
记忆管理器(电商客服场景)
每轮对话结束后,异步触发三类记忆的更新
"""


class MemoryManager:
    def __init__(self, session_manager: SessionManager):
        self.summary_agent = SummaryAgent(session_manager.summary_memory)
        self.window_memory = session_manager.window_memory
        self.long_agent = LongAgent(session_manager.long_memory)
        self.profile_agent = ProfileAgent(session_manager.profile_memory)

    async def update(self, user_id, question: str = ""):
        # 取出窗口记忆作为上下文素材
        query_window = await self.window_memory.query()
        if not query_window:
            return

        chat_text = ""
        for m in query_window:
            role = "用户" if m.get("role") == "user" else "客服"
            chat_text += f"{role}:{m.get('content', '')}\n"

        # 长期记忆
        await self.long_agent.update(user_id, chat_text)

        # 用户画像
        await self.profile_agent.update(chat_text)

        # 窗口满 2 轮后再触发摘要(避免无效摘要)
        if len(query_window) >= 2:
            await self.summary_agent.update(query_window)
