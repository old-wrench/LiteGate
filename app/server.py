# -*- coding: utf-8 -*-
"""FastAPI 应用装配：网关路由 + 管理面 API + 内置 Web 静态页面。"""

from __future__ import annotations

import ipaddress
import threading
import time

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigError, ConfigStore
from .db import StatsDB
from .gateway import Gateway, create_gateway_router
from . import admin

WEB_DIR = Path(__file__).resolve().parent / "static"


def create_app(config_path: str, db_path: str) -> FastAPI:
    store = ConfigStore(config_path)
    database = StatsDB(db_path)
    gateway = Gateway(store, database)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        _retention_stop.set()
        await gateway.aclose()
        database.close()
        store.close()

    app = FastAPI(
        title="LiteGate",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = store
    app.state.db = database
    app.state.gateway = gateway

    # 配置错误 -> 400（面板可直接展示 detail）
    @app.exception_handler(ConfigError)
    async def config_error_handler(_: Request, exc: ConfigError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # 兜底：未捕获异常返回 500 JSON，不对外暴露堆栈
    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception):
        return JSONResponse(
            {"detail": "服务器内部错误：" + exc.__class__.__name__}, status_code=500
        )

    app.include_router(create_gateway_router(gateway))
    app.include_router(admin.api)

    # ---- 用量日志自动清理（retention_days，默认 None = 不清理）------------
    # 启动时检查一次，之后每小时检查；面板「保存保留天数」时也会立即清理一次。
    _retention_stop = threading.Event()

    def _retention_loop() -> None:
        while True:
            try:
                days = store.snapshot().get("retention_days")
                if days:
                    n = database.purge_before(time.time() - days * 86400)
                    if n:
                        print("[db] 已自动清理 " + str(n) + " 条超过 "
                              + str(days) + " 天的用量记录", flush=True)
            except Exception as exc:
                print("[db] 用量日志自动清理失败：" + str(exc), flush=True)
            if _retention_stop.wait(3600.0):
                break

    threading.Thread(
        target=_retention_loop, name="usage-retention", daemon=True
    ).start()

    # 管理面访问来源控制（admin_access，随配置热加载，约2秒生效）：
    #   local     仅本机回环
    #   lan       本机 + 私有网段（首次启动默认）
    #   allowlist 本机 + 白名单（IP / CIDR / IPv4通配符）
    #   any       不限制（不推荐）
    # 回环来源在任何模式下都放行，保证本机不会把自己锁在门外。
    # /v1 数据面不受此限制，始终由虚拟Key鉴权。
    _LOCAL_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

    def _host_allowed(client: str, access: dict) -> bool:
        c = (client or "").strip("[]").lower()
        if not c:
            return False
        if c in _LOCAL_HOSTS:
            return True
        mode = (access or {}).get("mode", "lan")
        if mode == "any":
            return True
        try:
            ip = ipaddress.ip_address(c)
        except ValueError:
            return False
        if ip.is_loopback:
            return True
        if mode == "local":
            return False
        if mode == "lan":
            return ip.is_private
        # allowlist：IP / CIDR / 通配符
        for raw in (access or {}).get("allow") or []:
            s = str(raw or "").strip()
            if not s:
                continue
            if "/" in s:
                try:
                    net = ipaddress.ip_network(s, strict=False)
                except ValueError:
                    continue
                if net.version == ip.version and ip in net:
                    return True
            elif "*" in s:
                parts, cur = s.split("."), c.split(".")
                if (len(parts) == 4 and len(cur) == 4
                        and all(p == "*" or p == cur[i] for i, p in enumerate(parts))):
                    return True
            else:
                try:
                    if ipaddress.ip_address(s) == ip:
                        return True
                except ValueError:
                    continue
        return False

    @app.middleware("http")
    async def admin_access_guard(request: Request, call_next):
        path = request.url.path
        if path == "/" or path.startswith(("/admin", "/static")):
            # ---- 跨站写请求防护（CSRF）----
            # 管理面没有登录鉴权（本地自用设计），而浏览器发起的跨站 POST/PUT/DELETE
            # 一定携带 Origin/Referer 且其 host 与本站 Host 不同 —— 据此拦截，
            # 防止局域网里其他机器浏览器中打开的恶意网页静默调用 /admin 写接口改配置。
            # 命令行工具（curl 等）不携带这些头，不受影响；面板同源请求 host 一致，放行。
            if (path.startswith("/admin")
                    and request.method in ("POST", "PUT", "DELETE", "PATCH")):
                src = (request.headers.get("origin")
                       or request.headers.get("referer") or "")
                if src:
                    src_host = urlsplit(src).netloc.lower()
                    site_host = (request.headers.get("host") or "").lower()
                    if src_host and site_host and src_host != site_host:
                        return JSONResponse(
                            {"detail": "已拦截跨站写请求（Origin/Referer 与本站不一致）"},
                            status_code=403,
                        )
            client = (request.client.host if request.client else "") or ""
            access = app.state.store.snapshot().get("admin_access") or {}
            if not _host_allowed(client, access):
                return JSONResponse(
                    {"detail": "管理面板不允许从 " + (client or "未知来源")
                     + " 访问；请在本机打开面板，于「系统设置 → 管理面板访问」调整"},
                    status_code=403,
                )
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(
            WEB_DIR / "index.html", headers={"Cache-Control": "no-store"}
        )

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.middleware("http")
    async def no_store_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.state.created_default = store.created_default
    return app
