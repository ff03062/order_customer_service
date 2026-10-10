from langchain.tools import tool
from dotenv import load_dotenv
from email.mime.text import MIMEText
import smtplib
from app.tool.schema.email_schema import EmailParams

# 读取配置文件
import os

load_dotenv()


# 定义工具
@tool("send_email_tool", args_schema=EmailParams)
def send_email_tool(to: str, subject: str, content: str) -> str:
    """
    发送邮件，发送通知，发送消息
    """
    try:
        # 读取配置文件信息
        email = os.getenv("EMAIL_TO")
        host = os.getenv("EMAIL_HOST")
        port = os.getenv("EMAIL_PORT")
        password = os.getenv("EMAIL_PASSWORD")
        # 判断配置是否读取成功
        if not to or not host or not port or not password:
            return "邮件配置读取失败"
        # 创建邮件对象
        msg = MIMEText(content)
        # 收件人
        msg["To"] = to
        # 主题
        msg["Subject"] = subject
        # 发件人
        msg["From"] = email
        # 创建一个链接邮件服务器的地址
        with  smtplib.SMTP_SSL(host, int(port)) as smtp:
            # 登录邮件服务器
            smtp.login(email, password)
            # 发邮件
            smtp.sendmail(email, to, msg.as_string())
        return "邮件发送成功"
    except Exception as e:
        print(f"邮件发送异常{e}")
        return "邮件发送异常"


if __name__ == "__main__":
    rs = send_email_tool.invoke({
        "to":"2069985674@qq.com",
        "subject":"测试",
        "content":"这是一封测试邮件"
    })
    print(rs)
    # rs = send_email_tool.stream({
    #     "to": "2069985674@qq.com",
    #     "subject": "测试",
    #     "content": "这是一封测试邮件"
    # })
    # print(rs)
