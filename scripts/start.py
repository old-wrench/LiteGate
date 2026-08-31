# -*- coding: utf-8 -*-
"""LiteGate 一键启动脚本：环境准备 -> 前端构建校验 -> 拉起服务。

双击 start.bat 即等价于运行本脚本。流程：
1. （可选）在 .venv 中创建虚拟环境并安装依赖（首次自动执行，之后增量跳过）；
2. 前端「编译」阶段：校验静态资源完整性与 JS 语法（有 Node 时用 node --check），
   生成 app/static/.build-info.json 构建清单（含 SHA256 与时间戳）；
3. 以前台方式启动网关（Ctrl+C 停止），可选 --open 自动打开浏览器面板。

用法：
    python scripts/start.py                 # 标准一键启动
    python scripts/start.py --open          # 启动后自动打开面板
    python scripts/start.py --listen 127.0.0.1:9000
    python scripts/start.py --no-venv       # 直接用当前 Python，不建虚拟环境
    python scripts/start.py --reinstall     # 强制重装依赖
    python scripts/start.py --skip-build    # 跳过前端构建校验
其余参数原样透传给 main.py（--config / --db / --log-level 等）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
FRONTEND_FILES = ["index.html", "app.js", "style.css"]
BUILD_INFO = STATIC / ".build-info.json"
REQ_MARKER = ".requirements-ok"


def info(msg: str) -> None:
    print("[start] " + msg, flush=True)


def die(msg: str) -> int:
    print("[start][错误] " + msg, flush=True)
    return 1


# ---------------------------------------------------------------------------
# 1. Python 环境与依赖
# ---------------------------------------------------------------------------

def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def setup_deps(use_venv: bool, force_reinstall: bool) -> tuple:
    """准备运行 Python；必要时创建虚拟环境并安装依赖。

    返回 (python可执行路径, 是否本次新装了依赖)。
    """
    cur = Path(sys.executable)

    def have_fastapi(py: Path) -> bool:
        try:
            r = subprocess.run(
                [str(py), "-c", "import fastapi,uvicorn,httpx,yaml"],
                capture_output=True, timeout=60,
            )
            return r.returncode == 0
        except Exception:
            return False

    if not use_venv:
        return cur, False

    vp = venv_python(ROOT)
    marker = ROOT / ".venv" / REQ_MARKER
    need_install = force_reinstall or not marker.exists()
    if vp.exists() and not need_install and have_fastapi(vp):
        return vp, False

    if not vp.exists():
        info("首次运行：创建虚拟环境 .venv ...")
        r = subprocess.run([str(cur), "-m", "venv", str(ROOT / ".venv")])
        if r.returncode != 0 or not vp.exists():
            die("创建虚拟环境失败；可改用 --no-venv 直接以系统 Python 运行")
            sys.exit(1)

    if need_install or not have_fastapi(vp):
        info("安装依赖（fastapi/uvicorn/httpx/pyyaml）...")
        r = subprocess.run(
            [str(vp), "-m", "pip", "install", "-r",
             str(ROOT / "requirements.txt"), "--disable-pip-version-check", "-q"],
        )
        if r.returncode != 0:
            die("依赖安装失败，请检查网络后重试（或 --reinstall）")
            sys.exit(1)
        marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        return vp, True

    return vp, False


# ---------------------------------------------------------------------------
# 2. 前端「编译」阶段：完整性 + 语法校验 + 构建清单
# ---------------------------------------------------------------------------

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_frontend(skip: bool) -> int:
    if skip:
        info("跳过前端构建（--skip-build）")
        return 0

    info("前端构建开始：校验静态资源 ...")
    # 完整性：文件存在且非空、HTML 引用的资源齐全
    for name in FRONTEND_FILES:
        p = STATIC / name
        if not p.exists() or p.stat().st_size == 0:
            return die("前端资源缺失或为空：" + str(p))
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for ref in ("/static/app.js", "/static/style.css"):
        if ref not in html:
            return die("index.html 未引用必需资源：" + ref)

    # 语法校验：优先 node --check；没有 Node 则给出跳过说明（不影响使用）
    js = STATIC / "app.js"
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(js)], capture_output=True)
        if r.returncode != 0:
            print(r.stderr.decode("utf-8", "ignore")[-800:], flush=True)
            return die("app.js 语法校验未通过，已中止启动")
        checked = "node --check 通过"
    else:
        checked = "跳过（未检测到 Node.js；面板为原生JS不受影响）"

    manifest = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checker": checked,
        "files": {n: {"sha256": sha256(STATIC / n),
                      "bytes": (STATIC / n).stat().st_size}
                  for n in FRONTEND_FILES},
    }
    BUILD_INFO.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    info("前端构建完成 ✔ " + checked +
         "；清单 -> " + str(BUILD_INFO.relative_to(ROOT)))
    return 0


# ---------------------------------------------------------------------------
# 3. 端口占用检查
# ---------------------------------------------------------------------------

def warn_if_port_busy(hostport: str) -> None:
    host, _, port = (hostport or "").rpartition(":")
    if not port.isdigit():
        return
    sock = None
    try:
        s = __import__("socket").socket()
        s.settimeout(0.4)
        sock = s
        target = ("127.0.0.1" if host.strip("[]") in ("", "localhost")
                  else host.strip("[]"), int(port))
        if s.connect_ex(target) == 0:
            info("警告：端口 " + port + " 已被占用（可能已有 LiteGate 在跑）。"
                 "本次启动若绑定失败请先结束旧进程。")
            s.close()
            sock = None
    except Exception:
        pass
    finally:
        if sock is not None:
            sock.close()


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--no-venv", action="store_true")
    ap.add_argument("--reinstall", action="store_true")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器面板")
    args, forwarded = ap.parse_known_args()

    print("=" * 58)
    print("  LiteGate 一键启动")
    print("  Python : " + platform.python_version() + " @ " + sys.executable)
    print("=" * 58, flush=True)

    if sys.version_info < (3, 9):
        return die("需要 Python 3.9 及以上")

    py, freshly = setup_deps(not args.no_venv, args.reinstall)
    info("运行解释器：" + str(py) + ("（虚拟环境%s）" %
          ("新建" if freshly else "就绪") if not args.no_venv else "（--no-venv）"))

    rc = build_frontend(args.skip_build)
    if rc != 0:
        return rc

    listen = "127.0.0.1:8000"
    if "--listen" in forwarded:
        i = forwarded.index("--listen")
        if i + 1 < len(forwarded):
            listen = forwarded[i + 1]
    else:
        cfgp = ROOT / "config.yaml"
        if cfgp.exists():
            try:
                import yaml  # venv 已装好；此处仅读取一行配置
                d = yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}
                listen = str(d.get("listen_addr") or listen)
            except Exception:
                pass
    warn_if_port_busy(listen)

    if args.open:
        def _open_later():
            time.sleep(2.5)
            webbrowser.open("http://" + listen + "/")
        threading.Thread(target=_open_later, daemon=True).start()

    cmd = [str(py), str(ROOT / "main.py")] + forwarded
    info("启动服务：" + " ".join(cmd))
    print("-" * 58, flush=True)
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT))
        return proc.returncode
    except KeyboardInterrupt:
        info("收到 Ctrl+C，正在退出...")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
