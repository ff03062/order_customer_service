"""
FastAPI 入口
"""

import asyncio
import os
import sys

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse, RedirectResponse

from app.router.login_router import login_router

# 让 main.py 自身也能从项目根目录 import app.*
# 必须在所有 app.* import 之前执行
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_APP_DIR)
_HTML_DIR = os.path.join(_APP_DIR, "html")
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import redis
from redis import asyncio as redis_async
from fastapi import FastAPI
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from app.router.chat_router import chat_router
from app.agent.graph.customer_graph import CustomerServiceGraph
from app.agent.memory.save.summary_memory import pool

load_dotenv()
DB_URL = os.getenv("POSTGRESQL_URL")


# 定义异步上下文管理器
@asynccontextmanager
async def content_manager(app: FastAPI):
    """
    应用启动时：建立连接、创建表
    应用关闭时：自动关闭连接池
    """

    async with AsyncPostgresSaver.from_conn_string(DB_URL) as saver:
        # await saver.setup()  # 幂等，首次会建表
        r = redis_async.Redis(
            host="localhost",
            port=6379,
            db=0,
            password=os.getenv("REDIS_PASSWORD"),
        )
        app.state.order_agent = CustomerServiceGraph(saver, r)
        print("电商助手启动")         # 启动数据库连接池
        await pool.open()
        yield
    # 消耗对象
    app.state.exam_agent = None
    print("电商助手关闭")
    print("关闭：关闭 Postgres 连接池")
    await pool.close()


app = FastAPI(title="智能订单客服", lifespan=content_manager)

app.include_router(chat_router)
app.include_router(login_router)


# 页面路由（同域打开，前端 fetch('/sendEmail') 等才能打到本服务）
@app.get("/")
async def root():
    return RedirectResponse(url="/login")


@app.get("/login")
async def login_page():
    return FileResponse(os.path.join(_HTML_DIR, "login.html"))


@app.get("/chat")
async def chat_page():
    return FileResponse(os.path.join(_HTML_DIR, "chat.html"))


# 静态资源兜底（可选访问 /static/xxx.html）
app.mount("/static", StaticFiles(directory=_HTML_DIR), name="static")


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows 默认 Proactor 事件循环，psycopg 3.x 在异步模式下不支持，
        # 必须在创建任何连接之前切换到 Selector。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # 直接在本进程内启动，事件循环策略才会真正生效
    # （subprocess 再起一个 uvicorn 会丢掉上面设置的策略）
    import uvicorn
    import threading
    import webbrowser
    import time


    # 延迟 1.5 秒后自动打开浏览器
    def open_browser():
        print(f"🕐 子线程开始 sleep，{time.strftime('%H:%M:%S')}")
        time.sleep(0)
        print(f"🕐 子线程 sleep 结束，准备打开浏览器，{time.strftime('%H:%M:%S')}")
        webbrowser.open("http://127.0.0.1:8001/login")
        print(f"🕐 浏览器已打开，{time.strftime('%H:%M:%S')}")


    # 在后台线程中启动浏览器打开逻辑，避免阻塞主线程
    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8001,
        reload=False,
        log_level="info",
        loop=asyncio.SelectorEventLoop,
    )
