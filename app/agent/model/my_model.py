from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
from pathlib import Path
import os

# 读取配置文件：先尝试项目根目录的 .env，再尝试当前工作目录的 .env。
# 这样无论从哪个子目录启动 uvicorn，都能找到项目根目录的 .env。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=False)
load_dotenv()  # 兜底：当前工作目录的 .env
"""
基于单例模式的模型封装
"""
class MyModel:
    # 定义私有属性
    _model = None
    # 定义本地模型私有属性
    _local_model = None

    # 定义静态函数,在线模型
    @staticmethod
    def get_model():
        # 判断_model 是否未空
        if MyModel._model is None:
            api_key = os.getenv("DASHSCOPE_API_KEY")
            base_url = os.getenv("OPENAI_API_BASE")
            if not api_key:
                raise RuntimeError("DASHSCOPE_API_KEY 未配置，无法初始化云端模型")
            if not base_url:
                raise RuntimeError("OPENAI_API_BASE 未配置，无法初始化云端模型")
            # 创建模型
            MyModel._model = ChatOpenAI(
                model=os.getenv("MODEL_LINE_NAME"),
                api_key=api_key,
                base_url=base_url,
                streaming=True,  # 开启流式输出
                extra_body={
                    "enable_thinking": False  # 是否开启思考过程
                }
            )
        return MyModel._model

    # 本地模型创建
    @staticmethod
    def get_local_model():
        # 判断_model 是否未空
        if MyModel._local_model is None:
            # 创建模型
            MyModel._local_model = ChatOpenAI(
                model=os.getenv("MODEL_LOCAL_NAME"),
                api_key="ddd",
                base_url=os.getenv("LOCAL_URL"),
                streaming=True,  # 开启流式输出
                extra_body={
                    "enable_thinking": False  # 是否开启思考过程
                }
            )
        return MyModel._local_model