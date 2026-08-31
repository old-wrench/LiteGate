# -*- coding: utf-8 -*-
"""上游连通性诊断：读取 config.yaml 中指定(或第一条)上游，直接探测上游端点。

用途：区分「网关问题」与「上游问题」，并对 bigmodel 自动对比标准端点与
CodingPlan 专属端点（/api/coding/paas/v4）的响应差异。

用法：
    python scripts/diag_upstream.py           # 探测第一条上游
    python scripts/diag_upstream.py --alias glm-5.3
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.yaml")

# bigmodel 标准 <-> CodingPlan 专属 端点对照，便于自动对比
BIGMODEL_VARIANTS = {
    "standard": "https://open.bigmodel.cn/api/paas/v4",
    "coding": "https://open.bigmodel.cn/api/coding/paas/v4",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default=None)
    args = ap.parse_args()

    with open(CONFIG, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    ups = doc.get("upstreams") or []
    up = None
    if args.alias:
        up = next((u for u in ups if u["alias"] == args.alias), None)
    elif ups:
        up = ups[0]
    if up is None:
        print("config.yaml 中没有匹配的上游配置")
        return 1

    base = (up.get("api_base") or "").rstrip("/")
    key = up.get("api_key") or ""
    real_model = up.get("real_model")
    print("alias      : " + up["alias"])
    print("real_model : " + str(real_model))
    print("api_base   : " + base)
    print("api_key    : " + (key[:6] + "******" + key[-4:] if len(key) > 12 else "(未设置)"))
    print()

    targets = [base]
    if "open.bigmodel.cn" in base:
        variants = [v for k, v in BIGMODEL_VARIANTS.items() if v != base]
        targets.extend(variants)

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {
        "model": real_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "stream": False,
    }

    rc = 1
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=60, write=15, pool=10)) as c:
        for t in targets:
            url = t.rstrip("/") + "/chat/completions"
            print("== GET-ish probe -> " + url)
            try:
                r = c.post(url, json=payload, headers=headers)
            except Exception as exc:
                print("   连接失败: " + exc.__class__.__name__ + ": " + str(exc)[:200])
                continue
            body = r.text
            print("   HTTP " + str(r.status_code) + "  content-type=" +
                  r.headers.get("content-type", "?"))
            print("   body: " + (body[:600].replace("\n", " ") if body else "(空)"))
            print()
            if r.status_code == 200:
                rc = 0

    if rc == 0:
        print("[结论] 至少一个端点返回 200 —— 参考上面对比结果修正面板里的 api_base。")
    else:
        print("[结论] 所有候选端点均非 200。若各端点均为 429，多为账号侧限流/额度问题；"
              "密钥错端点也可能表现为 429/401。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
