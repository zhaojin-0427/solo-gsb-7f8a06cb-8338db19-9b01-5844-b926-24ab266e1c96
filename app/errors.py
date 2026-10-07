"""错误定义。

所有需要向用户精确定位的错误都携带文件（ZIP 内路径）与行列号。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LocatedError(Exception):
    """可定位的致命错误（包结构缺失、重名、越界、XML 解析失败等）。"""

    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        loc = self.location()
        return f"{loc}{self.message}" if loc else self.message

    def location(self) -> str:
        if not self.file:
            return ""
        loc = self.file
        if self.line is not None:
            loc += f":{self.line}"
            if self.column is not None:
                loc += f":{self.column}"
        return loc + ": "

    def to_dict(self) -> dict:
        return {
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "location": self.location(),
        }


class UploadError(Exception):
    """上传阶段的硬性限制错误（大小、篇数、加密等）。"""


class StructureErrors(Exception):
    """一个或多个可定位的包结构错误。"""

    def __init__(self, errors: list[LocatedError]):
        self.errors = errors
        super().__init__("; ".join(str(e) for e in errors))
