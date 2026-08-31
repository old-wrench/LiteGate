# -*- coding: utf-8 -*-
"""LiteGate 配置持久化与热加载。

设计要点：
- 上游渠道与全局设置保存为一个本地 YAML 文件；
- Web 面板的所有修改都会原子写回该文件并即时生效；
- 后台线程每 2 秒轮询文件 mtime，因此用编辑器手工改配置同样可以热加载，无需重启服务。
"""

from __future__ import annotations

import copy
import ipaddress
import os
import secrets
import tempfile
import threading
import uuid
from typing import Any, Callable, Optional

import yaml

DEFAULT_LISTEN_ADDR = "127.0.0.1:8000"
#: 支持在网关侧预设的请求体参数（会合并进转发给上游的 JSON body）
PARAM_KEYS = ("thinking_budget", "max_tokens", "max_context_tokens")
#: 管理面板访问来源模式（admin_access.mode）：
#:   local=仅本机 | lan=本机+私有网段(默认) | allowlist=本机+白名单 | any=不限制
ADMIN_ACCESS_MODES = ("local", "lan", "allowlist", "any")
DEFAULT_ADMIN_ACCESS = {"mode": "lan", "allow": []}


def _validate_allow_entry(s: str) -> None:
    """校验白名单条目：IP / CIDR 网段 / IPv4 通配符（192.168.0.*）。"""
    if "/" in s:
        try:
            ipaddress.ip_network(s, strict=False)
        except ValueError:
            raise ConfigError("白名单网段非法（应为 CIDR，如 10.0.0.0/8）：" + s) from None
        return
    if "*" in s:
        parts = s.split(".")
        if (len(parts) == 4
                and all(p == "*" or (p.isdigit() and 0 <= int(p) <= 255) for p in parts)):
            return
        raise ConfigError("通配符地址需形如 192.168.0.*：" + s)
    try:
        ipaddress.ip_address(s)
    except ValueError:
        # 来源永远是 IP，域名无法用于来源判断；如需域名接入请置于反向代理之后
        raise ConfigError("白名单条目需为 IP / 网段(CIDR) / 通配符：" + s) from None


def normalize_admin_access(raw: Any) -> dict:
    """校验并规整管理面板访问来源配置。回环来源在任何模式下都放行。"""
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw.get("mode") or DEFAULT_ADMIN_ACCESS["mode"]).strip().lower()
    if mode not in ADMIN_ACCESS_MODES:
        raise ConfigError("admin_access.mode 需为 " + "/".join(ADMIN_ACCESS_MODES))
    items_in = raw.get("allow") if isinstance(raw.get("allow"), list) else []
    allow: list = []
    seen: set = set()
    for it in items_in or []:
        s = str(it or "").strip()
        if not s or s in seen:
            continue
        _validate_allow_entry(s)
        seen.add(s)
        allow.append(s)
    return {"mode": mode, "allow": allow}


class ConfigError(ValueError):
    """配置非法错误，会转换为 HTTP 400 返回给 Web 面板展示。"""


# ---------------------------------------------------------------------------
# 解析 / 校验工具
# ---------------------------------------------------------------------------

def _opt_int(value: Any, field: str) -> Optional[int]:
    """把面板/文件里的值规整为「正整数 或 None（留空）」。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"{field} 应为正整数或留空")
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} 应为正整数或留空") from None
    if isinstance(value, float) and float(iv) != value:
        raise ConfigError(f"{field} 应为正整数或留空")
    if iv <= 0:
        raise ConfigError(f"{field} 必须大于 0")
    return iv


def normalize_upstream(item: Any) -> dict:
    """校验并规整一条上游渠道配置。多余字段会被丢弃，缺省字段补默认值。"""
    if not isinstance(item, dict):
        raise ConfigError("单条上游配置必须是键值映射（mapping）")

    def s(name: str, required: bool = False) -> str:
        v = item.get(name)
        if v is None:
            v = ""
        v = str(v).strip()
        if required and not v:
            raise ConfigError("字段 " + name + " 不能为空")
        return v

    alias = s("alias", True)
    real_model = s("real_model", True)
    api_base = s("api_base", True)
    if not api_base.lower().startswith(("http://", "https://")):
        raise ConfigError("api_base 需要以 http:// 或 https:// 开头：" + api_base)

    upstream: dict = {
        "id": str(item.get("id") or uuid.uuid4().hex[:12]),
        "alias": alias,
        "real_model": real_model,
        "api_base": api_base,
        "api_key": s("api_key"),
        "tag": s("tag"),
    }
    for f in PARAM_KEYS:
        upstream[f] = _opt_int(item.get(f), f)
    upstream["force_override_client_params"] = item.get(
        "force_override_client_params"
    ) in (True, "true", "True", 1)
    upstream["parse_stream_usage"] = item.get("parse_stream_usage") in (
        True,
        "true",
        "True",
        1,
    )
    return upstream


def normalize_keys(items: Any) -> list:
    """校验并规整一组对外分发的虚拟Key（发给不同同事使用）。"""
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ConfigError("api_keys 必须是列表")
    out = []
    seen_keys, seen_names = set(), set()
    for i, it in enumerate(items):
        if isinstance(it, str):
            it = {"key": it}
        if not isinstance(it, dict):
            raise ConfigError("第" + str(i + 1) + "条Key配置必须是键值映射")
        name = str(it.get("name") or "").strip()
        key = str(it.get("key") or "").strip()
        if not name:
            raise ConfigError(
                "第" + str(i + 1) + "把Key缺少名称（用于区分使用者并在统计中归组）"
            )
        if not key:
            raise ConfigError("Key「" + name + "」的值不能为空")
        if key in seen_keys:
            raise ConfigError("存在重复的Key值：" + key[:12] + "…")
        if name in seen_names:
            raise ConfigError("存在重复的Key名称：" + name)
        seen_keys.add(key)
        seen_names.add(name)
        raw_enabled = it.get("enabled", True)  # 缺省视为启用
        out.append({
            "id": str(it.get("id") or uuid.uuid4().hex[:12]),
            "name": name,
            "key": key,
            "enabled": raw_enabled in (True, "true", "True", 1),
        })
    return out


def normalize_doc(raw: Any) -> dict:
    """校验整个配置文档。任何不合法处抛出 ConfigError。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("YAML 根节点必须为键值映射（mapping）")
    listen_addr = (
        str(raw.get("listen_addr") or "").strip() or DEFAULT_LISTEN_ADDR
    )
    raw_keys = raw.get("api_keys")
    if not isinstance(raw_keys, list):
        # 兼容旧版单Key格式：自动迁移为 api_keys 列表（首次保存时落盘为新结构）
        legacy = str(raw.get("virtual_api_key") or "").strip()
        raw_keys = [{"name": "默认", "key": legacy, "enabled": True}] if legacy else []
    doc = {
        "listen_addr": listen_addr,
        "admin_access": normalize_admin_access(raw.get("admin_access")),
        "api_keys": normalize_keys(raw_keys),
        "upstreams": [],
    }
    seen: set = set()
    for u in raw.get("upstreams") or []:
        u = normalize_upstream(u)
        if u["alias"] in seen:
            raise ConfigError("alias 重复：" + u["alias"] + "（alias 是唯一的调用标识）")
        seen.add(u["alias"])
        doc["upstreams"].append(u)
    return doc


def parse_listen(addr: str):
    """把 '127.0.0.1:8000' 形式的监听地址解析为 (host, port)。

    允许任意合法监听地址：
    - 127.0.0.1 / ::1 / localhost —— 仅本机（默认，最安全）
    - 0.0.0.0 / :: —— 所有网卡（局域网模式：供同事调用 /v1）
    - 指定内网 IP —— 只绑定某块网卡
    安全边界与监听地址解耦：管理面（/、/admin、/static）在应用层
    始终强制仅回环访问（见 app/server.py 的 admin_local_guard 中间件），
    /v1 数据面由虚拟Key鉴权——放开监听不会暴露管理面板。
    """
    s = (addr or "").strip()
    if not s:
        raise ConfigError("listen_addr 不能为空，形如 127.0.0.1:8000")
    host, sep, port_s = s.rpartition(":")
    if not sep:
        raise ConfigError("listen_addr 需形如 127.0.0.1:8000，收到：" + addr)
    host = host.strip().strip("[]") or "127.0.0.1"
    try:
        port = int(port_s.strip())
    except ValueError:
        raise ConfigError("端口号非法：" + port_s) from None
    if not (1 <= port <= 65535):
        raise ConfigError("端口必须在 1~65535 之间：" + str(port))
    if host.lower() == "localhost":
        host = "127.0.0.1"
    elif host.lower() not in ("0.0.0.0", "::"):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # 允许主机名（交由 uvicorn 解析），拒绝明显乱写的字符串
            if not host.replace(".", "").replace("-", "").isalnum():
                raise ConfigError("listen_addr 地址/主机名非法：" + host) from None
    return host, port


# ---------------------------------------------------------------------------
# 配置存取器：内存快照 + 原子落盘 + mtime 热加载
# ---------------------------------------------------------------------------

class ConfigStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._data: dict = {}
        self._routes: dict = {}
        self._mtime: float = -1.0
        self._stop = threading.Event()

        created = False
        if not os.path.exists(self.path):
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            seed = {
                # 首次启动即局域网模式：同事可直接调用 /v1；
                # 管理面板默认允许 本机+局域网，可随时在「系统设置」收紧为仅本机/白名单。
                "listen_addr": "0.0.0.0:8000",
                "admin_access": {"mode": "lan", "allow": []},
                "api_keys": [
                    {"name": "默认", "key": "sk-virtual-" + secrets.token_urlsafe(18)}
                ],
                "upstreams": [],
            }
            self._atomic_dump(normalize_doc(seed))
            created = True

        with self._lock:
            self._adopt(self._parse_file())
        self._created = created

        threading.Thread(
            target=self._watch_loop, name="config-watcher", daemon=True
        ).start()

    # ---------------- 内部 ----------------
    def _parse_file(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return normalize_doc(raw)

    def _adopt(self, data: dict) -> None:
        self._data = data
        self._routes = {u["alias"]: u for u in data["upstreams"]}
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            pass

    def _atomic_dump(self, data: dict) -> None:
        payload = yaml.safe_dump(
            data, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(
            prefix=".litegate-config-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _watch_loop(self) -> None:
        """轮询 mtime，支持在面板之外用编辑器改 YAML 也实时生效。"""
        while not self._stop.wait(2.0):
            try:
                mt = os.stat(self.path).st_mtime
            except OSError:
                continue
            if abs(mt - self._mtime) < 1e-9:
                continue
            try:
                fresh = self._parse_file()
            except Exception as exc:  # 外部改坏了文件：保留旧配置继续服务
                print("[config] 配置文件解析失败，沿用内存中的旧配置：" + str(exc), flush=True)
                self._mtime = mt  # 避免同一份坏内容反复告警
                continue
            with self._lock:
                self._adopt(fresh)
            print(
                "[config] 检测到外部修改，已热加载（"
                + str(len(fresh["upstreams"]))
                + " 条上游配置）",
                flush=True,
            )

    # ---------------- 对外 ----------------
    def close(self) -> None:
        self._stop.set()

    @property
    def created_default(self) -> bool:
        return self._created

    def snapshot(self) -> dict:
        """返回整份配置的深拷贝快照（线程安全）。"""
        with self._lock:
            return copy.deepcopy(self._data)

    def route(self, alias: str):
        """按客户端传入的模型别名查找上游配置（找不到返回 None）。"""
        with self._lock:
            return self._routes.get(alias)

    def update(self, mapper: Callable[[dict], dict]) -> dict:
        """对配置做一次受控修改：mapper 收到深拷贝、返回新文档，统一校验后原子落盘。

        校验失败抛 ConfigError 时，磁盘与内存均保持不变。
        """
        with self._lock:
            draft = mapper(copy.deepcopy(self._data))
            fresh = normalize_doc(draft)
            self._atomic_dump(fresh)
            self._adopt(fresh)
            return copy.deepcopy(fresh)
