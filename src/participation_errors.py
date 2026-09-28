from __future__ import annotations

from typing import Any


class ParticipationReadError(RuntimeError):
    """无法可靠确认当前目标的交互状态；本次参与不得继续写操作。"""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        endpoint: str | None = None,
        dynamic_id: str | None = None,
    ) -> None:
        self.code = code
        self.message = str(message or "交互状态读取失败")
        self.endpoint = endpoint
        self.dynamic_id = str(dynamic_id) if dynamic_id is not None else None
        super().__init__(self.message)

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        endpoint: str | None,
        dynamic_id: str | None,
    ) -> "ParticipationReadError":
        code_value = payload.get("code") if isinstance(payload, dict) else None
        try:
            code = int(code_value) if code_value is not None else None
        except (TypeError, ValueError):
            code = None
        response_message = ""
        if isinstance(payload, dict):
            response_message = str(payload.get("message") or payload.get("msg") or "").strip()
        detail = f"交互状态读取失败 code={code}" if code is not None else "交互状态读取失败"
        if response_message:
            detail = f"{detail} message={response_message}"
        detail += "，已停止本次参与且不重试"
        error = cls(
            response_message or "交互状态读取失败",
            code=code,
            endpoint=endpoint,
            dynamic_id=dynamic_id,
        )
        error.args = (detail,)
        return error

