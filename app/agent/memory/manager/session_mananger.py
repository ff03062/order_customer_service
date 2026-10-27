from app.agent.memory.save.window_memory import WindowMemory
from app.agent.memory.save.summary_memory import SummaryMemory
from app.agent.memory.save.long_memory import LongMemory
from app.agent.memory.save.profile_memory import ProfileMemory
from app.agent.memory.manager.prompt_builder import PromptBuilder


"""
会话管理器(电商客服场景)
负责四层记忆的对象创建、对话落库以及提示词拼接
"""


class SessionManager:
    def __init__(self, session_id: str, user_id):
        self.session_id = session_id
        self.user_id = str(user_id)

        self.window_memory = WindowMemory(session_id)
        self.summary_memory = SummaryMemory(session_id)
        self.long_memory = LongMemory()
        self.profile_memory = ProfileMemory(user_id)
        # 复用上面四层记忆实例,避免 PromptBuilder 内再 new 一份
        self.prompt_builder = PromptBuilder(session_id, user_id, memories={
            "window": self.window_memory,
            "summary": self.summary_memory,
            "long": self.long_memory,
            "profile": self.profile_memory,
        })

    # 给窗口记忆写的保存方法
    async def save(self, role: str, content: str):
        await self.window_memory.save(role, content)

    # 给提示词构建器写的构建方法
    async def build_prompt(self, user_id, question: str) -> str:
        """返回拼好的记忆上下文（纯文本）。调用方决定放哪，这里不掺和消息结构。"""
        return await self.prompt_builder.builder_prompt(user_id, question)
