"""
B4 工具集(architecture.md §B4)。

分两层:
- 加密层(crypto.py):zfhe —— 数据/文件加解密
- 计算层:pandaseal / henumpy / helearn / hetorch

⚠️ 当前实现均为 STUB:用一个伪密文格式 ({"_marker": "STUB_CIPHER", "_plaintext": ...})
   模拟密文,让工作流可被端到端测试。等用户提供真实 SDK 后,各工具的 run/encrypt/decrypt
   函数体替换为真实调用即可,接口保持不变。
"""

from client.tools.crypto import CryptoToolkit, ZFHE  # ZFHE 为向后兼容别名
from client.tools.helearn import HELearn
from client.tools.henumpy import HENumpy
from client.tools.hetorch import HETorch
from client.tools.pandaseal import PandaSeal

__all__ = ["CryptoToolkit", "ZFHE", "PandaSeal", "HENumpy", "HELearn", "HETorch"]
