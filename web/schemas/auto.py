"""自动调度器请求模型。"""

from pydantic import BaseModel


class NightContinueRequest(BaseModel):
    enabled: bool
