"""产品里没有离线版：自然语言口令必须经过大模型。大模型没接入就明确报错，不悄悄退回规则。
（ZHIHUI_LLM=mock 的规则路径只给自动化测试和开发用。）"""
from __future__ import annotations

NO_KEY_MESSAGE = "还没有接入大模型：请点右上角「⚙ 设置」填写 API Key（如 DeepSeek），保存后重试。"


class LLMUnavailable(Exception):
    """大模型用不了：没填 API Key、接口失败、或给不出合格的计划。"""

    def __init__(self, message: str, reason: str = "api_error"):
        super().__init__(message)
        self.reason = reason
