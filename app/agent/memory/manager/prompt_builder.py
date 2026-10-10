from app.agent.memory.save.window_memory import WindowMemory
from app.agent.memory.save.summary_memory import SummaryMemory
from app.agent.memory.save.long_memory import LongMemory
from app.agent.memory.save.profile_memory import ProfileMemory


"""
提示词构建器(电商客服场景)
将四层记忆拼成最终发给 LLM 的 system prompt
"""


class PromptBuilder:
    def __init__(self, session_id, user_id, memories: dict = None):
        # 传入 memories 时复用外部实例,避免重复建立 Redis/PG 连接
        if memories:
            self.window_memory = memories["window"]
            self.summary_memory = memories["summary"]
            self.long_memory = memories["long"]
            self.profile_memory = memories["profile"]
        else:
            self.window_memory = WindowMemory(session_id)
            self.summary_memory = SummaryMemory(session_id)
            self.long_memory = LongMemory()
            self.profile_memory = ProfileMemory(user_id)

    async def builder_prompt(self, user_id, question: str) -> str:
        prompt = """
一、角色:你是一名电商平台客服助手
二、任务:
  - 理解用户当前问题
  - 结合下方的用户记忆信息(短期/摘要/长期/画像)给出个性化回答
三、规则:
  - 你必须只输出最终回答,禁止输出"Memory""Reply""用户姓名"等任何标签或记忆复述
  - 称呼用户时使用用户画像里的 name 字段,未知时直接说"您"
  - 不编造订单信息,无法回答时引导用户提供订单号
"""

        # 当前问题
        prompt += f"\n【当前问题】{question}\n"

        # 1. 窗口记忆
        window = await self.window_memory.query()
        prompt += "\n【最近对话】"
        if window:
            for m in window:
                role = "用户" if m.get("role") == "user" else "客服"
                prompt += f"\n  {role}:{m.get('content', '')}"
        else:
            prompt += "\n  (无)"

        # 2. 摘要记忆
        summary = await self.summary_memory.query()
        prompt += f"\n\n【会话摘要】{summary or '(无)'}"

        # 3. 长期记忆(按当前问题检索相关偏好)
        long_hits = await self.long_memory.query(user_id, question)
        prompt += "\n【历史偏好】"
        if long_hits:
            for x in long_hits:
                prompt += f"\n  - {x}"
        else:
            prompt += "\n  (无)"

        # 4. 用户画像
        profile = await self.profile_memory.query()
        prompt += f"\n\n【用户画像】\n{profile or '(无)'}"

        return prompt
