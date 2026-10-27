"""
LangGraph 图定义
"""
import asyncio
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
from app.agent.node.chat_node import chat_node
from app.agent.memory.manager import SessionManager, MemoryManager

FALLBACK_MSG = "抱歉，我这边暂时遇到了一点问题，已为您切换到对话模式，请换个方式告诉我您想了解什么～"

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


async def after_sales_node_with_fallback(state: CustomerServiceState):
    """
    售后节点异常时跳 chat。
    必须是 async：节点内部要调支付宝 MCP（网络请求）。
    LangGraph 会自动识别协程函数，主图用 astream 调用时无需额外改动。
    """
    try:
        result = await after_sales_node(state)
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
        graph.add_node("chat", chat_node)

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
        # -----------------------本轮记忆上下文-------------------------
        # 记忆构建失败时本轮无记忆，不打断对话
        session_manager = SessionManager(session_id, user_id)
        try:
            await session_manager.save("user", question)
            memory_context = await session_manager.build_prompt(user_id, question)
        except Exception as e:
            print(f"[memory] 构建记忆失败，本轮无记忆: {type(e).__name__}: {e}")
            memory_context = ""

        ai_answer = ""
        # 构建用户问题
        #
        # 注意（查询层状态共享）：
        #   这里只传 messages / user_id / user_email / memory_context。
        #   current_order_id、order_context 等字段都是「无 reducer 的标量通道」，
        #   本轮输入里没有它们时，LangGraph 会保留上一轮 checkpoint 的值 —— 
        #   这正是订单/售后子图能在下一轮拿到「上一笔订单」的原因。
        #   反过来说：不能在这里把它们写成默认空字符串，否则会覆盖掉上一轮的实体。
        #
        # 记忆上下文走独立的 memory_context 通道，不塞进 messages：
        #   messages 是被 Checkpointer 持久化的对话回合，塞进去会每轮重复一份、越滚越大。
        user_msg = {
            "messages": [HumanMessage(content=question)],
            "user_id": user_id,
            "user_email": "",
            "memory_context": memory_context,
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
        # 放后台跑：它要连调 3 次 LLM（长期/画像/摘要）。若在这里 await，
        # 前端得等它跑完才收到 done 事件，输入框会一直处于锁定状态。
        asyncio.create_task(self._update_memory(session_manager, user_id, question, ai_answer))

    async def _update_memory(self, session_manager, user_id, question, ai_answer):
        """后台更新三层记忆。失败只记日志，不影响已经返给用户的答复。"""
        try:
            await session_manager.save("ai", ai_answer)
            await MemoryManager(session_manager).update(user_id, question)
        except Exception as e:
            print(f"[memory] 更新记忆失败: {type(e).__name__}: {e}")

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