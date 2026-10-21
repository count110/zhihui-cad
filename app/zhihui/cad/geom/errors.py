"""几何内核的错误：纯代码、无依赖，工具层把它转换成 ToolError(code, message, hint)。"""
from __future__ import annotations


class GeomError(Exception):
    def __init__(self, code: str, message: str, hint: str = "", **extra):
        super().__init__(message)
        self.code, self.message, self.hint, self.extra = code, message, hint, extra
