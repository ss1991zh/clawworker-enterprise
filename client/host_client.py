"""用户端访问管理端的唯一 HTTP/TLS 传输层。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from client import host_trust


class HostConnectionError(ConnectionError):
    pass


class HostResponseError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class HostRequestCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class HostSession:
    base_url: str
    token: str


def _response_detail(response: httpx.Response, fallback: str) -> tuple[Any, str]:
    try:
        body = response.json()
    except Exception:
        body = {"detail": "管理端返回了无法识别的响应"}
    detail = body.get("detail", fallback) if isinstance(body, dict) else fallback
    return body, str(detail)


class HostClient:
    """统一处理Token、TLS锁定、系统代理隔离、超时和管理端错误。"""

    def __init__(
        self,
        session_provider: Callable[[], HostSession],
        *,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._session_provider = session_provider
        self._client_factory = client_factory
        self._clients: dict[tuple[str, str], httpx.Client] = {}
        self._lock = threading.RLock()

    def _client_for(self, base_url: str) -> httpx.Client:
        revision = host_trust.trust_revision(base_url)
        key = (base_url, revision)
        with self._lock:
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            # 证书重锁后关闭同一主机的旧连接，新建连接必须使用新锁定证书。
            stale = [item for item in self._clients if item[0] == base_url]
            for stale_key in stale:
                try:
                    self._clients.pop(stale_key).close()
                except Exception:  # noqa: BLE001
                    pass
            client = self._client_factory(
                base_url=base_url,
                verify=host_trust.verify_for(base_url),
                trust_env=False,
            )
            self._clients[key] = client
            return client

    def reset(self, base_url: str = "") -> None:
        """证书重锁或主机切换后丢弃旧连接。"""
        normalized = base_url.rstrip("/")
        with self._lock:
            keys = [
                key for key in self._clients
                if not normalized or key[0] == normalized
            ]
            for key in keys:
                try:
                    self._clients.pop(key).close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        self.reset()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        timeout: float = 60.0,
    ) -> httpx.Response:
        session = self._session_provider()
        base_url = session.base_url.rstrip("/")
        try:
            response = self._client_for(base_url).request(
                method,
                f"/{path.lstrip('/')}",
                json=json_body,
                headers={"Authorization": f"Bearer {session.token}"},
                # 保留旧客户端的快速连接失败语义：管理端不可达时 5 秒内
                # 返回，但数据查询/模型调用仍可使用更长的读写超时。
                timeout=httpx.Timeout(
                    timeout, connect=5.0, read=timeout, write=timeout, pool=timeout,
                ),
            )
        except httpx.RequestError as exc:
            self.reset(base_url)
            raise HostConnectionError(f"无法连接管理端：{type(exc).__name__}") from exc
        return response

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        timeout: float = 60.0,
    ) -> Any:
        response = self._request(
            method, path, json_body=json_body, timeout=timeout,
        )
        body, detail = _response_detail(response, "管理端请求失败")
        if response.status_code >= 400:
            raise HostResponseError(response.status_code, detail)
        return body

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 60.0,
    ) -> bytes:
        response = self._request(method, path, timeout=timeout)
        if response.status_code >= 400:
            _, detail = _response_detail(response, "管理端请求失败")
            raise HostResponseError(response.status_code, detail)
        return response.content


def cancellable_post(
    url: str,
    *,
    headers: dict,
    json_body: dict,
    timeout: float,
    should_cancel: Optional[Callable[[], bool]] = None,
    poll_interval: float = 0.2,
    before_request: Optional[Callable[[], None]] = None,
) -> httpx.Response:
    """可取消的管理端POST；Pipeline与普通API共用相同TLS和代理策略。"""
    if before_request:
        before_request()
    check_cancel = should_cancel or (lambda: False)
    verify = host_trust.verify_for(url)
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["response"] = httpx.post(
                url,
                headers=headers,
                json=json_body,
                timeout=httpx.Timeout(
                    timeout, connect=10.0, read=timeout, write=timeout, pool=timeout,
                ),
                verify=verify,
                trust_env=False,
            )
        except Exception as exc:  # 请求线程通过主线程重新抛出
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True, name="host-post")
    thread.start()
    while thread.is_alive():
        if check_cancel():
            raise HostRequestCancelled("用户已停止")
        thread.join(timeout=poll_interval)
    if "error" in box:
        raise box["error"]
    return box["response"]


def cancellable_json_post(
    url: str,
    *,
    headers: dict,
    json_body: dict,
    timeout: float,
    should_cancel: Optional[Callable[[], bool]] = None,
    poll_interval: float = 0.2,
    before_request: Optional[Callable[[], None]] = None,
) -> Any:
    """可取消 POST 的统一 JSON/错误语义。"""
    try:
        response = cancellable_post(
            url,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
            should_cancel=should_cancel,
            poll_interval=poll_interval,
            before_request=before_request,
        )
    except httpx.RequestError as exc:
        raise HostConnectionError(f"无法连接管理端：{type(exc).__name__}") from exc
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise HostResponseError(502, "管理端返回了无法识别的响应") from exc
    if response.status_code >= 400:
        detail = body.get("detail", "管理端请求失败") if isinstance(body, dict) else "管理端请求失败"
        raise HostResponseError(response.status_code, str(detail))
    return body
