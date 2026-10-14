from pydantic import BaseModel,Field

class MySQLSchema(BaseModel):
    """MySQL 工具参数格式"""
    sql: str = Field(description="要执行的 SQL 查询语句")
    user_id: int = Field(description="用户ID")

