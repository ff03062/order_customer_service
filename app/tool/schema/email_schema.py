from pydantic import BaseModel,Field

# 定义工具参数的检验类
class EmailParams(BaseModel):
    # 收件人
    to: str = Field(..., description="收件人邮箱")
    # 邮件主题
    subject: str = Field(..., description="邮件主题")
    # 正文
    content: str = Field(..., description="邮件正文")