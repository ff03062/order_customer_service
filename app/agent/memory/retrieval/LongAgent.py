from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from app.agent.model.my_model import MyModel
from app.agent.memory.save.long_memory import LongMemory

"""
长期记忆智能体(电商客服场景)
从客服对话中提取值得长期保存的偏好信息
"""


class LongAgent:
    def __init__(self, long_memory: LongMemory):
        self.model = MyModel.get_model()
        self.prompt = self.get_prompt()
        self.agent = self.get_agent()
        self.long_memory = long_memory

    def get_prompt(self):
        return """
一、角色：你是电商客服记忆提取助手
二、任务：从客服与用户的对话中,提取值得长期记忆的用户偏好信息
三、规则:
  1、记录:常买品类、尺码偏好、收货时间偏好、常用快递、支付习惯、明确表达的不满等
  2、不记录:寒暄、一次性的临时问题、订单号等具体业务编号
  3、每条记忆一句话,主语明确(如"用户偏好...",不要重复)
  4、没值得记录的内容时返回空字符串
四、输出:只输出记忆内容,不要任何前缀、解释或标点
"""

    def get_agent(self):
        return create_agent(
            model=self.model,
            system_prompt=self.prompt,
            tools=[],
            debug=False,
        )

    async def update(self, user_id, conversation: str):
        rs = await self.agent.ainvoke(
            {"messages": [HumanMessage(content=conversation)]}
        )
        extracted = (rs["messages"][-1].content or "").strip()
        if extracted:
            await self.long_memory.save(user_id, extracted)


if __name__ == "__main__":
    import asyncio

    async def main():
        m = LongMemory()
        agent = LongAgent(m)
        await agent.update(
            "u_1001",
            "用户: 我平时穿 L 码\nAI: 好的已记录\n用户: 麻烦帮我发顺丰",
        )

    asyncio.run(main())
