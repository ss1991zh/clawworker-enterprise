"""
B4 工具集 — v4 简化:只保留加密/解密层 + skills + runtime。

ops 包装类(PandaSeal/HENumpy/HELearn/HETorch)已删除 —— 新架构由 skills.py
直接调用 ps.* / hp.* / hl.* 即可,不再需要 wrapper。
"""

from client.tools.crypto import CryptoToolkit, ZFHE  # ZFHE 为向后兼容别名

__all__ = ["CryptoToolkit", "ZFHE"]
