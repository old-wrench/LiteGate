# -*- coding: utf-8 -*-
"""从客户端视角完整验证网关（支持非流式/流式）。

用法：
    python scripts/probe_gateway.py [alias] [max_tokens] [stream]
"""

from __future__ import annotations

import json
import os
import sys

import httpx
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(ROOT, "config.yaml"), "r", encoding="utf-8") as f:
    doc = yaml.safe_load(f)

# 跟随配置文件的监听地址，避免换端口后脚本失效
_listen = str(doc.get("listen_addr") or "127.0.0.1:8000").strip()
_host = _listen.rsplit(":", 1)[0].strip("[]") or "127.0.0.1"
if _host in ("", "localhost", "0.0.0.0"):
    _host = "127.0.0.1"
GW = "http://" + _host + ":" + _listen.rsplit(":", 1)[1]

raw_keys = doc.get("api_keys") or []
vkey = ((raw_keys[0].get("key") if raw_keys else "")
        or doc.get("virtual_api_key") or "").strip()
print("使用Key   : " + (raw_keys[0]["name"] if raw_keys else "(旧版单Key)"))
ups = doc.get("upstreams") or []
if not ups:
    print("没有上游配置")
    sys.exit(1)
up = ups[0]
alias = sys.argv[1] if len(sys.argv) > 1 else up["alias"]
max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 32
want_stream = len(sys.argv) > 3 and sys.argv[3] == "stream"

print(("流式" if want_stream else "非流式") + " 探测别名 : " + alias)
body = {"model": alias,
        "messages": [{"role": "user", "content": "用一句话介绍你自己"}],
        "max_tokens": max_tokens}
if want_stream:
    body["stream"] = True

hdrs = {"Authorization": "Bearer " + vkey}
timeout = httpx.Timeout(connect=10, read=300, write=15, pool=10)

with httpx.Client(timeout=timeout) as c:
    if want_stream:
        with c.stream("POST", GW + "/v1/chat/completions", json=body, headers=hdrs) as r:
            print("HTTP", r.status_code, "| content-type:", r.headers.get("content-type", "?"))
            n_chunks, tail = 0, ""
            for line in r.iter_lines():
                if not line.strip():
                    continue
                n_chunks += 1
                tail = line
            print("SSE 分片行数:", n_chunks, "| 末行:", tail[:120])
    else:
        r = c.post(GW + "/v1/chat/completions", json=body, headers=hdrs)
        print("HTTP", r.status_code)
        try:
            data = r.json()
        except Exception:
            print(r.text[:300]);  sys.exit(1)
        if r.status_code == 200:
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if not content and msg.get("reasoning_content"):
                content = "(仅有思考内容) " + msg["reasoning_content"][:60]
            print("回复 :", content[:80])
        else:
            print(json.dumps(data, ensure_ascii=False)[:300])

    s = c.get(GW + "/admin/stats/logs", params={"limit": 2})
    rows = s.json()["rows"]
    print()
    print("最新统计（新→旧）:")
    for row in rows:
        print("  alias=%s tag=%s 流式=%s prompt=%s completion=%s cached=%s"
              % (row["alias"], row["upstream_tag"], row["is_stream"],
                 row["prompt_tokens"], row["completion_tokens"], row["cached_tokens"]))
