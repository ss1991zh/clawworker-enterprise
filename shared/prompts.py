"""
加载 LLM 系统 prompt。

系统 prompt 的权威来源是 ~/llm_system_prompt.md(本仓库外的设计文档)。
本模块负责读取并提取其中 ` ``` ... ``` ` 包裹的实际 prompt 块。

设计上把 prompt 从代码里分离出来,方便:
- 设计文档与代码同步(改文档即改 prompt)
- 测试时直接 mock 这一层
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# 设计文档查找顺序:repo 内 docs > 用户家目录(MVP 兼容旧布局)
_REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DOC_CANDIDATES = [
    _REPO_ROOT / "docs" / "llm_system_prompt.md",
    Path.home() / "llm_system_prompt.md",
]
PROMPT_DOC_PATH = next((p for p in PROMPT_DOC_CANDIDATES if p.exists()), PROMPT_DOC_CANDIDATES[0])


def _extract_first_code_block(markdown_text: str) -> Optional[str]:
    """从 markdown 文本中提取第一个 ``` ... ``` 代码块的内容。"""
    pattern = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
    m = pattern.search(markdown_text)
    return m.group(1).strip() if m else None


def load_system_prompt(path: Optional[Path] = None) -> str:
    """
    从设计文档加载 System Prompt。

    Args:
        path: 自定义文档路径(测试用)。默认读取 ~/llm_system_prompt.md。

    Returns:
        提取出的 system prompt 文本。

    Raises:
        FileNotFoundError: 文档不存在
        ValueError: 文档中未找到代码块
    """
    doc_path = path or PROMPT_DOC_PATH
    if not doc_path.exists():
        raise FileNotFoundError(
            f"LLM 系统 prompt 文档不存在: {doc_path}。"
            f"请确认 ~/llm_system_prompt.md 已生成。"
        )

    text = doc_path.read_text(encoding="utf-8")
    block = _extract_first_code_block(text)
    if not block:
        raise ValueError(f"在 {doc_path} 中未找到 ``` 代码块包裹的 prompt")
    return block


def build_user_message(user_query: str, schema_json: str) -> str:
    """
    把用户问题 + schema 包装成发给 LLM 的 user message。

    重要:schema_json 是 SchemaDescription 的序列化,绝不含具体数据。
    """
    return (
        f"用户问题:\n{user_query}\n\n"
        f"数据 schema(只有元数据,无明文):\n{schema_json}\n\n"
        f"请按 system prompt 要求,输出 computation_plan + summary。"
    )
