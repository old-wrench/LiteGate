#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LiteGate 启动入口。

用法：
    python main.py                        # 默认 ./config.yaml + ./usage.db + 127.0.0.1:8000
    python main.py --listen 127.0.0.1:9000
    python main.py --config my.yaml --db my.db
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LiteGate 极简LLM代理网关")
    p.add_argument("--config", default=os.path.join(ROOT, "config.yaml"),
                   help="YAML 配置文件路径（默认 ./config.yaml）")
    p.add_argument("--db", default=os.path.join(ROOT, "usage.db"),
                   help="SQLite 统计库路径（默认 ./usage.db）")
    p.add_argument("--listen", default=None,
                   help="监听地址，形如 127.0.0.1:8000（覆盖配置文件中的 listen_addr，一次性生效不落盘）")
    p.add_argument("--log-level", default="info",
                   choices=["critical", "error", "warning", "info", "debug"],
                   help="uvicorn 日志级别")
    p.add_argument("--no-access-log", action="store_true",
                   help="关闭访问日志（更安静）")
    return p


def mask(key: str) -> str:
    if not key:
        return "(未设置)"
    return key[:12] + "..." if len(key) > 16 else key


def lan_ips() -> list:
    """探测本机内网 IPv4（UDP connect 技巧，只取路由源地址，不实际发包）。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
        finally:
            s.close()
    except OSError:
        pass
    return ips


def main() -> int:
    args = build_parser().parse_args()

    from app.config import ConfigError, parse_listen
    from app.server import create_app

    cfg_listen = args.listen
    try:
        host, port = parse_listen(cfg_listen or "")
    except ConfigError:
        # 命令行未指定时回落到配置文件里的 listen_addr；仍需是回环地址
        cfg_listen = None

    app = create_app(args.config, args.db)

    from app.config import parse_listen as _pl
    listen_src = cfg_listen or (app.state.store.snapshot()["listen_addr"])
    try:
        host, port = _pl(listen_src)
    except ConfigError as exc:
        print("[启动失败] 监听地址非法：" + str(exc))
        return 2

    snap = app.state.store.snapshot()
    enabled_keys = [k for k in snap.get("api_keys", []) if k.get("enabled")]
    # 0.0.0.0/:: 只是监听通配地址，不是浏览器可访问的地址；横幅展示用回环地址
    panel_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    panel = "http://" + panel_host + ":" + str(port) + "/"
    print("=" * 58)
    print("  LiteGate · 极简LLM代理网关")
    print("  面板地址 : " + panel)
    print("  API 地址 : " + panel.rstrip("/") + "/v1/chat/completions")
    print("  分发Key  : 共 " + str(len(snap.get("api_keys", []))) + " 把，启用 "
          + str(len(enabled_keys)) + " 把（面板中管理/复制）")
    _acc = snap.get("admin_access") or {}
    _mode_txt = {"local": "仅本机", "lan": "本机+局域网",
                 "allowlist": "白名单 " + str(len(_acc.get("allow") or [])) + " 条",
                 "any": "不限制"}.get(str(_acc.get("mode")), "未知")
    print("  面板访问  : " + _mode_txt + "（本机永远允许；系统设置中可调）")
    print("  上游数量 : " + str(len(snap["upstreams"])))
    print("  配置文件 : " + app.state.store.path)
    print("  统计库   : " + app.state.db.path)
    if getattr(app.state, "created_default", False):
        print("  * 已生成初始配置文件（含随机虚拟Key），请在面板中完善渠道。")
    if host in ("127.0.0.1", "::1"):
        print("  * 仅监听本机回环地址，不对局域网开放。Ctrl+C 退出。")
    else:
        ips = " 或 ".join(lan_ips()) or "<本机内网IP>"
        print("  * 局域网模式：/v1 已对局域网开放（虚拟Key鉴权）；管理面板访问策略见上方「面板访问」。")
        print("    局域网同事侧配置：OPENAI_BASE_URL=http://" + ips + ":" + str(port) + "/v1")
    print("=" * 58, flush=True)

    import uvicorn
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=args.log_level,
        access_log=not args.no_access_log,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
