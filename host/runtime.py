"""管理端运行时组件容器。

模块导入阶段只创建轻量代理；SQLite、账户文件、DPAPI 和查询线程池统一在
FastAPI lifespan 启动阶段初始化。这样导入路由、构建安装包和运行静态检查时
不会意外创建真实用户数据。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Generic, TypeVar

from host.admin_auth import AdminAuth
from host.cert_manager import AuthorizationManager
from host.data_access import DataAccessStore
from host.data_sources import DataSourceStore
from host.db_connectors import ConnectorRegistry
from host.dispatcher import Dispatcher
from host.llm_configs import CallStatStore, LLMConfigStore, ProviderManager
from host.login_throttle import LoginThrottle
from host.query_gateway import QueryGateway
from host.query_tasks import QueryTaskManager
from host.user_manager import UserManager


T = TypeVar("T")


class LazyComponent(Generic[T]):
    """线程安全的透明延迟代理。"""

    def __init__(self, factory: Callable[[], T], name: str) -> None:
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_instance", None)
        object.__setattr__(self, "_lock", threading.RLock())

    @property
    def initialized(self) -> bool:
        return object.__getattribute__(self, "_instance") is not None

    def get(self) -> T:
        instance = object.__getattribute__(self, "_instance")
        if instance is not None:
            return instance
        with object.__getattribute__(self, "_lock"):
            instance = object.__getattribute__(self, "_instance")
            if instance is None:
                instance = object.__getattribute__(self, "_factory")()
                object.__setattr__(self, "_instance", instance)
        return instance

    def __getattr__(self, name: str):
        return getattr(self.get(), name)

    def __repr__(self) -> str:
        state = "ready" if self.initialized else "pending"
        return f"<LazyComponent {object.__getattribute__(self, '_name')} {state}>"


class HostRuntime:
    """管理端单进程组件图，并负责有序启停。"""

    def __init__(self) -> None:
        self.started_at = 0.0
        self.startup_duration_ms = 0
        self.last_startup_error = ""
        self.auth_manager = LazyComponent(AuthorizationManager, "authorization-manager")
        self.user_manager = LazyComponent(
            lambda: UserManager(self.auth_manager.get()), "user-manager",
        )
        self.dispatcher = LazyComponent(Dispatcher, "dispatcher")
        self.llm_config_store = LazyComponent(LLMConfigStore, "llm-config-store")
        self.provider_manager = LazyComponent(
            lambda: ProviderManager(self.llm_config_store.get()), "provider-manager",
        )
        self.call_stats = LazyComponent(CallStatStore, "call-stats")
        self.data_source_store = LazyComponent(DataSourceStore, "data-source-store")
        self.connector_registry = LazyComponent(ConnectorRegistry, "connector-registry")
        self.data_access_store = LazyComponent(
            lambda: DataAccessStore(self.data_source_store.get().db_path), "data-access-store",
        )
        self.query_gateway = LazyComponent(
            lambda: QueryGateway(
                self.data_source_store.get(),
                self.data_access_store.get(),
                self.connector_registry.get(),
            ),
            "query-gateway",
        )
        self.query_task_manager = LazyComponent(
            lambda: QueryTaskManager(
                self.query_gateway.get(),
                self.data_source_store.get(),
                self.data_access_store.get(),
            ),
            "query-task-manager",
        )
        self.admin_auth = LazyComponent(AdminAuth, "admin-auth")
        self.login_throttle = LazyComponent(LoginThrottle, "login-throttle")

    def startup(self) -> None:
        # 按依赖顺序主动触发：任一组件失败都让服务启动失败，
        # 而不是等用户点到对应页面才暴露。
        started = time.monotonic()
        try:
            for component in self.components():
                component.get()
        except Exception as exc:
            self.last_startup_error = type(exc).__name__
            raise
        self.started_at = time.time()
        self.startup_duration_ms = int((time.monotonic() - started) * 1000)
        self.last_startup_error = ""

    def components(self) -> tuple[LazyComponent, ...]:
        return (
            self.auth_manager,
            self.user_manager,
            self.dispatcher,
            self.llm_config_store,
            self.provider_manager,
            self.call_stats,
            self.data_source_store,
            self.connector_registry,
            self.data_access_store,
            self.query_gateway,
            self.query_task_manager,
            self.admin_auth,
            self.login_throttle,
        )

    def readiness(self) -> dict:
        pending = [
            object.__getattribute__(component, "_name")
            for component in self.components()
            if not component.initialized
        ]
        database_ok = False
        if not pending:
            try:
                with self.data_source_store.get()._connect() as connection:
                    database_ok = connection.execute("SELECT 1").fetchone()[0] == 1
            except Exception:  # noqa: BLE001
                database_ok = False
        ready = not pending and database_ok and not self.last_startup_error
        return {
            "status": "ready" if ready else "starting",
            "service": "host",
            "database": "ok" if database_ok else "unavailable",
            "pending_components": len(pending),
            "startup_duration_ms": self.startup_duration_ms,
            "started_at": self.started_at,
            "startup_error": self.last_startup_error,
        }

    def shutdown(self) -> None:
        if self.query_task_manager.initialized:
            self.query_task_manager.get().close()
