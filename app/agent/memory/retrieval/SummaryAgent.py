from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from app.agent.model.my_model import MyModel
from app.agent.memory.save.summary_memory import SummaryMemory

"""
摘要智能体(电商客服场景)
根据旧摘要 + 本轮对话生成新摘要
"""


class SummaryAgent:
    def __init__(self, summary_memory: SummaryMemory):
        self.model = MyModel.get_local_model()
        self.prompt = self.get_prompt()
        self.agent = self.get_agent()
        self.summary_memory = summary_memory

    def get_prompt(self):
        return """
一、角色：你是电商客服会话摘要助手
二、任务:根据旧摘要和最新对话,生成新的会话摘要
三、规则：
  1、记录:用户问过的关键业务问题、已处理的诉求、未解决的问题
  2、去掉闲聊和寒暄
  3、使用第三人称客观陈述
  4、2000字以内,不重复
  5、只返回新摘要,不要任何额外文字
"""

    def get_agent(self):
        return create_agent(
            model=self.model,
            system_prompt=self.prompt,
            tools=[],
            debug=False,
        )

    async def update(self, messages: list[dict]):
        old_summary = await self.summary_memory.query()
        chat_lines = []
        for m in messages:
            role = "用户" if m.get("role") == "user" else "客服"
            chat_lines.append(f"{role}:{m.get('content', '')}")

        question = f"旧摘要:{old_summary}\n\n本轮对话:\n" + "\n".join(chat_lines)
        rs = await self.agent.ainvoke({"messages": [HumanMessage(content=question)]})
        new_summary = (rs["messages"][-1].content or "").strip()
        if new_summary:
            await self.summary_memory.save(new_summary)


if __name__ == "__main__":
    import asyncio

    async def main():
        m = SummaryMemory("order_001")
        agent = SummaryAgent(m)
        msgs = [
            {"role": "user", "content": "我的订单还没发货"},
            {"role": "ai", "content": "已为您查询,订单正在出库中"},
            {"role": "user", "content": "大概什么时候能到"},
        ]
        await agent.update(msgs)
        print(await m.query())

    asyncio.run(main())
