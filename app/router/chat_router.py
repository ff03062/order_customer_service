"""
聊天路由 - SSE 流式输出
"""
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel


chat_router = APIRouter()

class SessionSchema(BaseModel):
    session_id: str = None
    user_id: str = None


@chat_router.post("/create_session")
async def create_session(request: Request, session: SessionSchema):
    print(f"用户ID:{session.user_id},会话ID:{session.session_id}")
    order_agent = request.app.state.order_agent
    # -------------验证会话是否存在的逻辑-----------------------
    is_exist, msg = await order_agent.check_session(session.user_id, session.session_id)

    # 前端没带 session_id：直接给一个新的 UUID
    if not session.session_id:
        new_id = await order_agent.create_session(session.user_id)
        return {"code": 200, "msg": "新建会话", "data": new_id}
    elif not is_exist:
        print("会话不存在！创建新会话")
        session_id = await order_agent.create_session(session.user_id)
        print(f"新会话ID:{session_id}")
        return {"code": 200, "msg": "会话不存在！创建新会话", "data": session_id}
    else:
        print("会话存在！")
        return {"code": 200, "msg": "会话存在！", "data": session.session_id}


@chat_router.get("/chat/stream")
async def chat(request: Request, question: str, user_id: str, session_id: str = ""):
    """
    SSE 聊天接口（页面路由占用 /chat，流式接口使用 /chat/stream）

    Args:
        question: 用户问题
        user_id: 用户ID
        session_id: 会话ID（可选；为空或不存在时自动创建）
    """
    print(f"用户问题:{question},用户ID:{user_id},会话ID:{session_id}")
    order_agent = request.app.state.order_agent

    # 前端没带 session_id 时,自动建一个,避免 thread_id 为空导致记忆串味
    if not session_id:
        session_id = await order_agent.create_session(user_id)
        print(f"[chat] 未传 session_id,自动创建: {session_id}")

    async def generate():
        """SSE 流式生成器"""
        try:
            # 流式输出
            async for content in order_agent.chat(question, user_id, session_id):
                if content:
                    data = {"data": content, "done": False}
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            # 结束标记
            data = {"data": "", "done": True}
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        except Exception as e:
            import traceback
            print(f"流式输出异常: {e}\n{traceback.format_exc()}")
            data = {"data": f"系统错误: {str(e)}", "done": True}
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
