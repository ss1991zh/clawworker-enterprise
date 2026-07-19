"""
登录限速 / 锁定 —— 防在线爆破。

进程内内存计数(host 单进程,够用):按 key(用户名或来源IP)累计失败,
达阈值后锁定一段时间,锁定窗口随连续失败指数增长(封顶)。登录成功即清零。
"""
from __future__ import annotations

import threading
import time

_FAIL_THRESHOLD = 5        # 连续失败多少次开始锁定
_BASE_LOCK_SEC = 30.0      # 首次锁定秒数
_LOCK_FACTOR = 2.0         # 每再超一次失败,锁定翻倍
_LOCK_CAP_SEC = 900.0      # 锁定上限 15 分钟
_RESET_AFTER_SEC = 900.0   # 距上次失败超过此时长,计数自然清零


class LoginThrottle:
    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, int] = {}
        self._last: dict[str, float] = {}
        self._locked_until: dict[str, float] = {}

    def _now(self) -> float:
        return time.monotonic()

    def check(self, key: str) -> float:
        """返回该 key 还需等待的秒数(>0 表示当前被锁定,应拒绝登录);0 表示可尝试。"""
        with self._lock:
            now = self._now()
            # 长时间无活动 → 自然清零
            if key in self._last and now - self._last[key] > _RESET_AFTER_SEC:
                self._fails.pop(key, None)
                self._locked_until.pop(key, None)
            until = self._locked_until.get(key, 0.0)
            return max(0.0, until - now)

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._now()
            self._last[key] = now
            n = self._fails.get(key, 0) + 1
            self._fails[key] = n
            if n >= _FAIL_THRESHOLD:
                over = n - _FAIL_THRESHOLD
                lock = min(_BASE_LOCK_SEC * (_LOCK_FACTOR ** over), _LOCK_CAP_SEC)
                self._locked_until[key] = now + lock

    def record_success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._last.pop(key, None)
            self._locked_until.pop(key, None)
