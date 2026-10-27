@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist ".env" (
    echo [提示] 没有找到 .env，请先复制 .env.example 为 .env 并填写配置。
    echo.
    pause
    exit /b 1
)

echo 正在启动电商客服 Agent ...
python app/main.py
pause
