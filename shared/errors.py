"""跨管理端/用户端的错误分类；对外只暴露稳定代码和安全描述。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum


class ErrorCategory(str, Enum):
    NETWORK = "network"
    PERMISSION = "permission"
    DATA = "data"
    MODEL = "model"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ClassifiedError:
    category: ErrorCategory
    code: str
    public_message: str


_MESSAGES = {
    ErrorCategory.NETWORK: "网络连接失败，请检查管理端地址和网络后重试",
    ErrorCategory.PERMISSION: "当前账户没有完成此操作的权限",
    ErrorCategory.DATA: "输入数据或返回结果格式不符合要求",
    ErrorCategory.MODEL: "模型服务暂时无法完成请求，请稍后重试",
    ErrorCategory.CANCELLED: "操作已取消",
    ErrorCategory.INTERNAL: "程序内部错误，请凭请求号查看日志",
}


def classify_exception(exc: BaseException) -> ClassifiedError:
    name = type(exc).__name__.lower()
    if "cancel" in name:
        category = ErrorCategory.CANCELLED
    elif isinstance(exc, PermissionError) or "permission" in name or "denied" in name:
        category = ErrorCategory.PERMISSION
    elif isinstance(exc, (ConnectionError, TimeoutError)) or any(
        marker in name for marker in ("connect", "network", "timeout", "http")
    ):
        category = ErrorCategory.NETWORK
    elif isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
        category = ErrorCategory.DATA
    elif any(marker in name for marker in ("llm", "model", "provider")):
        category = ErrorCategory.MODEL
    else:
        category = ErrorCategory.INTERNAL
    return ClassifiedError(
        category=category,
        code=f"{category.value}_error",
        public_message=_MESSAGES[category],
    )
