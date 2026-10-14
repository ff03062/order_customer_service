"""
LangGraph 图定义
"""
import contextlib
import json
import os
import time
import uuid
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from langchain_core.messages import AIMessage, HumanMessage
from app.agent.state.customer_state import CustomerServiceState
from app.agent.node.manager_node import manager_node
from app.agent.node.intent_node import intent_node
from app.agent.node.order_node import order_node
from app.agent.node.after_sales_node import after_sales_node
from app.agent.node.chat_node import chat_node_stream
from app.agent.memory.manager import SessionManager, MemoryManager

FALLBACK_MSG = "抱歉，我这边暂时遇到了一点问题，已为您切换到对话模式，请换个方式告诉我您想了解什么～"
TEST_SESSION_ID = os.getenv("TEST_SESSION_ID", "111")  # docs 测试用，可由环境变量覆盖


def order_node_with_fallback(state: CustomerServiceState):
    """order 节点异常时跳 chat。"""
    try:
        result = order_node(state)
        return Command(update=result, goto=END)
    except Exception as e:
        import traceback
        print(f"[fallback] order_node 失败: {e}")
        print(traceback.format_exc())
        return Command(
            update={"messages": [AIMessage(content=FALLBACK_MSG)], "current_step": "fallback"},
            goto="chat",
        )


def after_sales_node_with_fallback(state: CustomerServiceState):
    """after_sales 节点异常时跳 chat。"""
    try:
        result = after_sales_node(state)
        return Command(update=result, goto=END)
    except Exception as e:
        import traceback
        print(f"[fallback] after_sales_node 失败: {e}")
        print(traceback.format_exc())
        return Command(
            update={"messages": [AIMessage(content=FALLBACK_MSG)], "current_step": "fallback"},
            goto="chat",
        )


class CustomerServiceGraph:
    def __init__(self, checkpoint, redis):
        self.checkpointer = checkpoint
        self.redis = redis
        self.graph = self.build_graph()

    def build_graph(self):
        graph = StateGraph(CustomerServiceState)

        graph.add_node("manager", manager_node)
        graph.add_node("intent", intent_node)
        graph.add_node("order", order_node_with_fallback)
        graph.add_node("after_sales", after_sales_node_with_fallback)
        graph.add_node("chat", chat_node_stream)

        graph.add_edge(START, "intent")
        graph.add_edge("intent", "manager")
        graph.add_edge("chat", END)

        return graph.compile(checkpointer=self.checkpointer)

    async def create_session(self, user_id, session_id: str = ""):
        """
        创建会话。
        如果传入了 session_id（写死模式），复用并保证 Redis 里有记录；
        否则生成 UUID。
        """
        if session_id:
            key = session_id
        else:
            key = f"session:" + uuid.uuid4().hex
        if self.redis:
            data = {"user_id": user_id, "create_time": time.time()}
            await self.redis.set(key, json.dumps(data), ex=60 * 60 * 24)
        return key

    async def check_session(self, user_id, session_id):
        print(f"检查会话,会id:{session_id}")
        if session_id:
            # 获取会话数据
            data = await self.redis.get(session_id)
            print(f"是否有会话:{data}")
            # 检查会话是否是该用户创建的或者是伪造的
            if not data:
                print("会话不存在")
                return False, "会话不存在"
            # 检查会话归属
            rs = json.loads(data)
            if rs["user_id"] != user_id:
                print("会话不属于该用户")
                return False, "会话不属于该用户"
            print("会话存在且归属该用户")
            return True, "会话存在且归属该用户"
        return False, "会话id为空"

    async def chat(self, question, user_id, session_id):
        # -----------------------添加记忆-------------------------
        # 创建会话管理器
        session_manager = SessionManager(session_id, user_id)
        # 记忆构建失败时降级为纯对话,不能打断主流程
        try:
            # 添加窗口记忆
            await session_manager.save("user", question)
            # 构建记忆的提示词
            memory_prompt = await session_manager.build_prompt(user_id, question)
        except Exception as e:
            print(f"[memory] 构建记忆失败,降级为无记忆对话: {e}")
            memory_prompt = ""


        ai_answer = ""
        # 构建用户问题
        #
        # 注意（查询层状态共享）：
        #   这里只传 messages / user_id / user_email。
        #   current_order_id、order_context 等字段都是「无 reducer 的标量通道」，
        #   本轮输入里没有它们时，LangGraph 会保留上一轮 checkpoint 的值 —— 
        #   这正是订单/售后子图能在下一轮拿到「上一笔订单」的原因。
        #   反过来说：不能在这里把它们写成默认空字符串，否则会覆盖掉上一轮的实体。
        #
        # 记忆构建失败时 memory_prompt 为空串，不能塞进 messages（会污染消息列表）
        messages = [HumanMessage(content=question)]
        if memory_prompt:
            messages.insert(0, memory_prompt)

        user_msg = {
            "messages": messages,
            "user_id": user_id,
            "user_email": "",
        }
        # 配置检测点
        config = {"configurable": {"thread_id": session_id}}

        try:
            async with self._session_lock(session_id):
                print(f"开始处理会话:{session_id}")
                # 采用异步流式
                async for c, m in self.graph.astream(user_msg, config, stream_mode=["messages", "custom"]):
                    if c == "messages":
                        chunk, meta = m  # messages 模式下是 (MessageChunk, metadata) 二元组
                        if chunk.content:
                            ai_answer += chunk.content
                            yield chunk.content
                    else:
                        ai_answer += m
                        yield m
            print(f"AI回复:{ai_answer}")
        except Exception as e:
            import traceback
            print(f"错误:{e}\n{traceback.format_exc()}")
            yield str(e)
        # ---------------------更新记忆---------------------------
        # 记忆更新同样降级处理,不影响已产出的回答
        try:
            # 添加AI回复的记忆
            await session_manager.save("ai", ai_answer)
            # 更新记忆(记忆管理器里面传的是会话管理器对象)
            memory_manger = MemoryManager(session_manager)
            await memory_manger.update(user_id, question)
        except Exception as e:
            print(f"[memory] 更新记忆失败(已忽略): {e}")
        # ------------------------------------------------

    """
     会话锁：同一 session 同一时刻只允许一个请求进入。
    """

    @contextlib.asynccontextmanager
    async def _session_lock(self, session_id: str):

        key = f"lock:session:{session_id}"
        # 生成随机 token
        token = uuid.uuid4().hex
        # 尝试获取锁， px=60 * 1000 表示锁的过期时间，单位毫秒
        # nx=True 是 Redis SET 命令的一个选项，意思是：只有当这个 key 不存在时，才设置成功
        ok = await self.redis.set(key, token, nx=True, px=5 * 60 * 1000)
        # 获取锁成功，执行 yield 语句块中的代码
        if not ok:
            raise RuntimeError("会话忙，请稍后再试")

        try:
            # yield 的位置，就是"锁内要执行的所有代码"插入的地方。
            yield
        finally:
            # 兜底处理，确保锁一定能释放
            # 释放锁，使用 Lua 脚本确保原子性，防止误删，误释放
            lua = """
            if redis.call('get', KEYS[1]) == ARGV[1] then
                return redis.call('del', KEYS[1])
            else
                return 0
            end
            """
            try:
                # 执行 Lua 脚本，释放锁
                await self.redis.eval(lua, 1, key, token)
            except Exception as e:
                print(e)
                pass

    # 画图
    def draw(self):
        data = self.graph.get_graph().draw_mermaid_png()
        with open("考试图.png", "wb") as f:
            f.write(data)

if __name__ == '__main__':
    CustomerServiceGraph(1,2).draw()