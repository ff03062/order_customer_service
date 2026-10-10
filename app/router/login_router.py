import json
import os
import smtplib
from email.mime.text import MIMEText
import random

import redis
from pydantic import BaseModel, Field
from fastapi import APIRouter
from app.tool.mysql_tool import mysql_tool


# =============== 校验 ======================
class CheckCodeEntity(BaseModel):
    email: str = Field(..., title="邮箱号")
    code: str = Field(..., title="验证码")


# ================ 路由 ====================
login_router = APIRouter()


@login_router.get(
    path="/sendEmail",
    # 接口标题，显示在 Swagger UI 的接口列表中
    summary="发送邮箱",
    # 接口详细描述，显示在 Swagger UI 的接口详情中
    description="""
        给用户输入的邮箱号发送验证码
        请求参数：
            email：字符串类型，用户输入的邮箱号
        返回值：
            {
                "code": 状态码，成功200、失败500，int
                "msg": 提示信息，字符串
                "data": None，数据内容，Object
            }
    """,
)
def send_email(email: str):
    return send_email_service(email)


@login_router.post(
    path="/checkCode",
    summary="检查验证码",
    description="""
        检查用户输入的验证码是否正确
        请求参数：
            CheckCodeEntity：一个对象，2个属性：
                email：邮箱号，用于取出redis中存入的值作为key
                code：验证码，用户输入的
        返回值：
            {
                "code": 状态码，成功200、失败500，int
                "msg": 提示信息，字符串
                "data": None，数据内容，Object
            }
    """,
)
def check_code(checkCodeEntity: CheckCodeEntity):
    return check_code_service(checkCodeEntity)


# ========================  业务功能 =======================

def get_redis_conn():
    return redis.from_url(
        url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True
    )


def check_email(email: str):
    email = email.replace("'", "''")  # 防注入
    sql = f"select * from users where email='{email}'"
    result = mysql_tool.invoke({"sql": sql, "user_id": ""})
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return result


def send_email_service(email):
    # 返回结果是一个布尔值
    result = check_email(email)
    if not result:  # 空列表 / None / 空字符串都算不存在
        return {
            "code": 500,
            "msg": "邮箱不存在",
            "data": None
        }

    code = ''
    for i in range(4):
        code += str(int(random.random() * 10))

    # r 就是那个客户端实例
    r = get_redis_conn()
    # 现在把验证码存入到redis中
    r.setex(f"captcha:email:{email}", 60, code)

    # 配置发送信息
    subject = '来自 你的电商小助手~，你注册的登陆验证码是...：'
    content = f'你的登陆验证码是：{code}\n请60秒内使用，切勿泄露给他人'

    # ======
    try:
        # 读取配置文件信息
        sender = os.getenv("EMAIL_TO")
        host = os.getenv("EMAIL_HOST")
        port = os.getenv("EMAIL_PORT")
        password = os.getenv("EMAIL_PASSWORD")
        # 创建邮件对象
        msg = MIMEText(content)
        # 收件人
        msg["To"] = email
        # 发件人
        msg["From"] = sender
        # 主题
        msg["Subject"] = subject
        # 创建一个链接邮件服务器的地址
        with  smtplib.SMTP_SSL(host, int(port)) as smtp:
            # 登录邮件服务器
            smtp.login(sender, password)
            # 发邮件
            smtp.sendmail(sender, email, msg.as_string())
            smtp.quit()

            user_info = check_email(email)
            user_id = user_info[0]['user_id'] if user_info else None
            print("邮件发送成功")
            return {
                "code": 200,
                "msg": "验证码已发送",
                "data": user_id   # ← 返回数据库里的用户id
            }
    except Exception as e:
        print(f"邮件发送异常{e}")
        return {
            'code': 500,
            "msg": "发送失败",
            "data": None}
    # =============================


def check_code_service(checkCodeEntity: CheckCodeEntity):
    email = checkCodeEntity.email
    code = checkCodeEntity.code

    r = get_redis_conn()
    redis_code = r.get(f"captcha:email:{email}")

    if redis_code is None:
        return {
            "code": 500,
            "msg": "验证码已过期",
            "data": None,
        }
        # 进行验证码的比对
    if redis_code != code:
        return {
            "code": 500,
            "msg": "验证码不正确",
            "data": None,
        }

    result = check_email(email)

    return {
        "code": 200,
        "msg": "验证码正确",
        "data": {"user_id": result[0]["user_id"]}
    }


if __name__ == '__main__':
    email = '2069985674@qq.com'

    # 1. 发送验证码
    print("=== 1. 发送验证码 ===")
    send_result = send_email_service(email)
    print("发送结果：", send_result)

    # 2. 从 Redis 取回验证码（测试用，模拟用户收到邮件）
    print("\n=== 2. 从 Redis 取验证码 ===")
    r = get_redis_conn()
    code = r.get(f"captcha:email:{email}")
    print("Redis 里的验证码：", code)

    # 3. 校验验证码 + 取 user_id
    print("\n=== 3. 校验验证码 ===")
    test_entity = CheckCodeEntity(email=email, code=code)
    check_result = check_code_service(test_entity)
    print("校验结果：", check_result)
    print("取到的 user_id：", check_result.get("data"))
