"""
B3 本地存储(architecture.md §B3)。

- 密文数据 / 对话历史 / 长期记忆 → 客户端应用目录
- Excel 输出 → ~/Downloads/ (由 excel_output.py 处理)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

APP_DATA_DIR = Path.home() / ".agent-system"
CIPHERTEXT_DIR = APP_DATA_DIR / "ciphertexts"
HISTORY_DIR = APP_DATA_DIR / "history"


class LocalStorage:
    """简化的本地数据访问层。"""

    def __init__(self, root: Optional[Path] = None):
        self._root = root or APP_DATA_DIR
        self._ciphertext_dir = self._root / "ciphertexts"
        self._history_dir = self._root / "history"
        self._ciphertext_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)

    @property
    def ciphertext_dir(self) -> Path:
        return self._ciphertext_dir

    def save_ciphertext(self, name: str, blob: bytes) -> Path:
        path = self._ciphertext_dir / name
        path.write_bytes(blob)
        return path

    def load_ciphertext(self, name: str) -> bytes:
        return (self._ciphertext_dir / name).read_bytes()

    def list_ciphertexts(self) -> list[Path]:
        return sorted(self._ciphertext_dir.iterdir())
