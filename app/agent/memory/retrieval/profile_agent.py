from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.agent.model.my_model import MyModel
from app.agent.memory.save.profile_memory import ProfileMemory

"""
用户画像智能体(电商客服场景)
从对话中提取结构化的用户属性
"""


class CustomerProfileParams(BaseModel):
    name: str = Field(default="", description="姓名")
    age: int = Field(default=0, description="年龄,未提及填 0")
    gender: str = Field(default="", description="性别")
    default_address: str = Field(default="", description="常用收货地址关键词")
    preferred_payment: str = Field(default="", description="常用支付方式")
    preferred_courier: str = Field(default="", description="常用快递公司")
    shopping_categories: str = Field(default="", description="常买品类,逗号分隔")
    size_preference: str = Field(default="", description="尺码偏好")
    vip_level: str = Field(default="", description="会员等级,未提及留空")
    contact_info: str = Field(default="", description="联系方式补充")


class ProfileAgent:
    def __init__(self, profile_memory: ProfileMemory):
        self.model = MyModel.get_model()
        self.prompt = self.get_prompt()
        self.agent = self.get_agent()
        self.profile_memory = profile_memory

    def get_prompt(self):
        return """
一、角色:你是「电商客服」系统中的用户画像提取助手
二、任务:从用户与客服的对话中,抽取用户的属性,用于后续个性化服务
三、字段定义:
  1. name:姓名
  2. age:年龄(未提及填 0)
  3. gender:性别
  4. default_address:常用收货地址关键词
  5. preferred_payment:常用支付方式(微信/支付宝/银行卡 等)
  6. preferred_courier:常用快递公司(顺丰/中通/圆通 等)
  7. shopping_categories:常买品类(服装/3C/生鲜 等)
  8. size_preference:尺码偏好(如"L 码""偏大一号")
  9. vip_level:会员等级(普通/银牌/金牌/钻石)
  10. contact_info:补充联系方式
四、提取规则:
  1. 只提取能从对话中明确得到的信息,不要编造
  2. 未提及的字段:字符串填 "",整型填 0
  3. 使用第三人称客观陈述
  4. 同一信息只记录一次
  5. 没有画像信息时所有字段留空
五、输出:只输出结构化结果,不要任何额外文字
"""

    def get_agent(self):
        return create_agent(
            model=self.model,
            system_prompt=self.prompt,
            tools=[],
            debug=False,
            response_format=CustomerProfileParams,
        )

    async def update(self, conversation: str):
        try:
            rs = await self.agent.ainvoke(
                {"messages": [HumanMessage(content=conversation)]}
            )
            data = rs["structured_response"].model_dump()
            for key, value in data.items():
                if value not in (None, "", 0):
                    await self.profile_memory.save(key, value)
        except Exception as e:
            print(f"[ProfileAgent.update] error: {e}")


if __name__ == "__main__":
    import asyncio

    async def main():
        m = ProfileMemory("u_1001")
        agent = ProfileAgent(m)
        await agent.update(
            "用户: 我叫李雷,平时穿 L 码\n"
            "AI: 好的李雷,请问收货地址是?\n"
            "用户: 上海市浦东新区张江路 100 号,顺丰到付"
        )
        print(await m.query())

    asyncio.run(main())
