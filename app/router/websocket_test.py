from fastapi import WebSocket
from fastapi import APIRouter

ws_router = APIRouter()

#定义web接口
@ws_router.websocket("/ws")
async def test(ws:WebSocket):
    try:
        # 第一次握手，创建链接
        await ws.accept()
        print("第一次握手，创建链接")
        while True:
            # 获取客户端发送的数据
            data = await ws.receive_text()
            print("客户端发送数据:", data)
            # 服务端发送数据
            msg = "你好，我是服务端"
            await  ws.send_text(msg)
    except Exception as e:
        print("异常:", e)




