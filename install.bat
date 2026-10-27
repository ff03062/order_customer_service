@echo off
chcp 65001 >nul
setlocal

echo ============================================
echo   电商客服 Agent  -  依赖安装
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 python 命令。
    echo 请先安装 Python 3.10 或以上，并在安装时勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

python --version
echo.

echo [1/2] 升级 pip ...
python -m pip install --upgrade pip
echo.

echo [2/2] 安装依赖（首次较慢，sentence-transformers 会拉 PyTorch）...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [失败] 依赖安装出错。国内网络可以换清华镜像重试：
    echo   python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   安装完成
echo ============================================
echo 接下来三步：
echo   1. 复制 .env.example 为 .env，按 docs/环境变量说明.md 填写配置
echo   2. 准备 MySQL / PostgreSQL / Redis（建库建表脚本见文档）
echo   3. 双击 run.bat 启动，浏览器会自动打开 http://127.0.0.1:8001/login
echo.
pause
