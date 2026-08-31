# -*- coding: utf-8 -*-
"""LiteGate 端到端冒烟测试。

流程：
1. 在本进程内启动一个 mock 上游（OpenAI 兼容 /v1/chat/completions，支持流式、
   stream_options.include_usage 与工具调用模拟）；
2. 以子进程启动真正的网关服务（独立的临时配置/数据库/端口）；
3. 验证：多虚拟Key鉴权与停用、未知别名400、别名路由+model替换、参数优先级、
   错误原样透传且不脏统计、SSE直通记0、流式旁路采集 usage/cached/tool_calls、
   外部改 YAML 热加载、导入导出往返、聚合(含请求数/按使用者分组)、清空日志。

运行：python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MOCK_PORT = 18901
GW_PORT = 18413
GW = "http://127.0.0.1:%d" % GW_PORT
VKEY = "sk-vtest-main-0000000000"       # 主账号(默认)
K2 = "sk-vtest-second-00000000"         # 同事B
KOFF = "sk-vtest-disabled-000000"       # 已停用
HDR = {"Authorization": "Bearer " + VKEY}
HDR2 = {"Authorization": "Bearer " + K2}

RECEIVED = []


def build_mock_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        RECEIVED.append(body)
        msgs = body.get("messages") or []
        if msgs and msgs[0].get("content") == "FAIL":
            return JSONResponse({"error": {"message": "boom", "type": "upstream_boom"}},
                                status_code=500)
        usage_tail = (b'data: {"id":"cmpl-mock","object":"chat.completion.chunk",'
                      b'"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":45,'
                      b'"prompt_tokens_details":{"cached_tokens":7}}}\n\n')

        if body.get("stream"):
            async def gen():
                yield (b'data: {"id":"cmpl-mock","object":"chat.completion.chunk",'
                       b'"choices":[{"delta":{"role":"assistant","content":"hel"}}]}\n\n')
                await asyncio.sleep(0.02)
                yield (b'data: {"id":"cmpl-mock","object":"chat.completion.chunk",'
                       b'"choices":[{"delta":{"content":"lo"}}]}\n\n')
                if body.get("_smoke_tools"):
                    yield (b'data: {"id":"cmpl-mock","object":"chat.completion.chunk",'
                           b'"choices":[{"delta":{"tool_calls":[{"index":0,'
                           b'"id":"call_1","function":{"name":"f","arguments":"{}"}}]}}]}\n\n')
                    yield (b'data: {"id":"cmpl-mock","object":"chat.completion.chunk",'
                           b'"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n')
                if ((body.get("stream_options") or {}).get("include_usage")):
                    yield usage_tail
                yield b"data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")

        tool_part = {}
        finish = "stop"
        if body.get("_smoke_tools"):
            tool_part = {"tool_calls": [{"id": "call_1", "type": "function",
                                         "function": {"name": "f", "arguments": "{}"}},
                                        {"id": "call_2", "type": "function",
                                         "function": {"name": "g", "arguments": "{}"}}]}
            finish = "tool_calls"
        return {
            "id": "cmpl-mock",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "ok", **tool_part},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45,
                      "prompt_tokens_details": {"cached_tokens": 7}},
        }

    return app


def start_mock() -> uvicorn.Server:
    config = uvicorn.Config(build_mock_app(), host="127.0.0.1", port=MOCK_PORT,
                            log_level="error", access_log=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("mock upstream failed to start")
    return server


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def ok(name):
    print("  PASS  " + name, flush=True)


def chat_body(alias, **extra):
    body = {"model": alias,
            "messages": [{"role": "user", "content": "hi"}]}
    body.update(extra)
    return body


def fetch_logs(client):
    r = client.get("/admin/stats/logs", params={"limit": 50})
    expect(r.status_code == 200, "logs 接口失败: " + str(r.status_code))
    return r.json()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="litegate-smoke-")
    cfg_path = os.path.join(tmp, "config.yaml")
    db_path = os.path.join(tmp, "usage.db")
    out_path = os.path.join(tmp, "gw_stdout.log")

    doc = {
        "listen_addr": "127.0.0.1:%d" % GW_PORT,
        "api_keys": [
            {"name": "主账号", "key": VKEY},
            {"name": "同事B", "key": K2},
            {"name": "已离职", "key": KOFF, "enabled": False},
        ],
        "upstreams": [
            {"alias": "mock-basic", "real_model": "mock-real",
             "api_base": "http://127.0.0.1:%d/v1" % MOCK_PORT,
             "api_key": "mk-basic", "tag": "acct-a",
             "max_tokens": 222, "force_override_client_params": False},
            {"alias": "preset-soft", "real_model": "mock-real",
             "api_base": "http://127.0.0.1:%d/v1" % MOCK_PORT,
             "api_key": "mk-soft", "tag": "acct-b",
             "thinking_budget": 1000, "max_context_tokens": 128000},
            {"alias": "force-on", "real_model": "mock-real",
             "api_base": "http://127.0.0.1:%d/v1" % MOCK_PORT,
             "api_key": "mk-force", "tag": "acct-c",
             "thinking_budget": 2048, "max_tokens": 999,
             "max_context_tokens": 128000,
             "force_override_client_params": True},
            {"alias": "sse-tap", "real_model": "mock-real",
             "api_base": "http://127.0.0.1:%d/v1" % MOCK_PORT,
             "api_key": "mk-tap", "tag": "acct-tap",
             "parse_stream_usage": True},
        ],
    }
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)

    mock_server = start_mock()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    child = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py"),
         "--config", cfg_path, "--db", db_path,
         "--listen", "127.0.0.1:%d" % GW_PORT,
         "--log-level", "warning", "--no-access-log"],
        cwd=ROOT, env=env,
        stdout=open(out_path, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )

    # 记账：rows=入库行数 sum_tokens=token合计 sum_tools=工具调用合计 req_by_client
    acc = {"rows": 0, "tokens": 0, "tools": 0}

    def add_row(tokens=168, tools=0, client="主账号"):
        acc["rows"] += 1
        acc["tokens"] += tokens
        acc["tools"] += tools

    try:
        deadline = time.time() + 25
        while True:
            if child.poll() is not None:
                tail = open(out_path, encoding="utf-8").read()[-3000:]
                raise RuntimeError("网关进程提前退出，输出：\n" + tail)
            try:
                if httpx.get(GW + "/admin/meta", timeout=0.8).status_code == 200:
                    break
            except Exception:
                pass
            if time.time() > deadline:
                raise RuntimeError("网关启动超时")
            time.sleep(0.25)

        client = httpx.Client(base_url=GW, timeout=10)
        print("[smoke] 网关已就绪，开始用例", flush=True)

        # ---- 1. 多Key鉴权 -------------------------------------------------
        r = client.post("/v1/chat/completions", json=chat_body("mock-basic"),
                        headers={"Authorization": "Bearer wrong-key"})
        expect(r.status_code == 401, "未知Key应401，实际 " + str(r.status_code))
        r = client.post("/v1/chat/completions", json=chat_body("mock-basic"),
                        headers={"Authorization": "Bearer " + KOFF})
        expect(r.status_code == 401, "停用Key应401")
        r = client.post("/v1/chat/completions", json=chat_body("mock-basic"))
        expect(r.status_code == 401, "缺失Header应401")
        ok("鉴权：未知Key / 停用Key / 缺失Header 均 -> 401")

        # ---- 2. 未知别名 -> 400 ------------------------------------------
        r = client.post("/v1/chat/completions", json=chat_body("nope"), headers=HDR)
        expect(r.status_code == 400, "未知alias应得400")
        ok("路由：未知别名 -> 400")

        # ---- 3. 基本转发 + usage 入库 + 客户端参数优先 -------------------
        r = client.post("/v1/chat/completions",
                        json=chat_body("mock-basic", max_tokens=7), headers=HDR)
        expect(r.status_code == 200, "正常请求应200: " + r.text[:200])
        sent = RECEIVED[-1]
        expect(sent["model"] == "mock-real", "应替换为 real_model")
        expect(sent.get("max_tokens") == 7, "客户端参数优先")
        add_row()
        row = fetch_logs(client)["rows"][0]
        expect(row["prompt_tokens"] == 123 and row["completion_tokens"] == 45
               and row["cached_tokens"] == 7, "usage 应入库(123/45/缓存7)")
        ok("转发：别名替换 + usage(输入123/输出45/缓存7) 入库；客户端参数优先")

        # ---- 4. 多Key归属统计 --------------------------------------------
        r = client.post("/v1/chat/completions", json=chat_body("mock-basic"),
                        headers={"Authorization": "Bearer " + K2})
        expect(r.status_code == 200, "同事B Key应可用")
        add_row(client="同事B")
        rows = fetch_logs(client)["rows"]
        expect(rows[0]["client_name"] == "同事B", "归属应为同事B，实际 "
               + str(rows[0]["client_name"]))
        ok("多Key：第二把Key通过并正确归属到「同事B」")

        # ---- 5. 参数优先级三态 -------------------------------------------
        r = client.post("/v1/chat/completions",
                        json={"model": "preset-soft",
                              "messages": [{"role": "user", "content": "hi"}]},
                        headers=HDR)
        expect(r.status_code == 200, "preset-soft 应200")
        s = RECEIVED[-1]
        expect(s.get("thinking_budget") == 1000 and s.get("max_context_tokens") == 128000
               and "max_tokens" not in s, "预设注入不符")
        add_row()
        ok("参数：未传 -> 注入预设；未预设字段不注入")

        r = client.post("/v1/chat/completions",
                        json=chat_body("preset-soft", thinking_budget=555), headers=HDR)
        expect(RECEIVED[-1].get("thinking_budget") == 555, "客户端值应优先")
        add_row()
        ok("参数：关闭强覆盖 -> 客户端值(555)覆盖预设(1000)")

        r = client.post("/v1/chat/completions",
                        json=chat_body("force-on", thinking_budget=8, max_tokens=7),
                        headers=HDR)
        s = RECEIVED[-1]
        expect(s.get("thinking_budget") == 2048 and s.get("max_tokens") == 999
               and s.get("max_context_tokens") == 128000, "强覆盖失败")
        add_row()
        ok("参数：强覆盖开启 -> 全部替换为预设")

        rows_before = fetch_logs(client)["total"]
        expect(rows_before == acc["rows"], "入库行数%d != 记账%d" % (acc["rows"], rows_before))

        # ---- 6. 上游错误原样透传且不脏统计 --------------------------------
        r = client.post("/v1/chat/completions",
                        json={"model": "mock-basic",
                              "messages": [{"role": "user", "content": "FAIL"}]},
                        headers=HDR)
        expect(r.status_code == 500 and r.json()["error"]["message"] == "boom",
               "上游500应原样透传")
        expect(fetch_logs(client)["total"] == rows_before, "失败请求不应写库")
        ok("透传：上游500原文返回且不写库")

        # ---- 7. 纯直通流式：记0 + 流式标记 -------------------------------
        with client.stream("POST", "/v1/chat/completions",
                           json=chat_body("mock-basic", stream=True),
                           headers=HDR) as sr:
            expect(sr.status_code == 200 and
                   sr.headers.get("content-type", "").startswith("text/event-stream"),
                   "流式应200/event-stream")
            text = "".join(ln + "\n" for ln in sr.iter_lines())
        expect("[DONE]" in text and '"content":"hel"' in text, "SSE 内容不符")
        sent = RECEIVED[-1]
        expect("stream_options" not in sent, "未开采集不应注入 stream_options")
        newest = fetch_logs(client)["rows"][0]
        expect(newest["is_stream"] == 1 and newest["prompt_tokens"] == 0
               and newest["completion_tokens"] == 0 and newest["client_name"] == "主账号",
               "纯直通行应 全0+流式标记+主账号归属")
        add_row(tokens=0)
        ok("流式直通：SSE 原样透传；不注入 stream_options；token 记0")

        # ---- 8. 旁路采集流式：usage + 缓存 + 工具调用次数 ------------------
        with client.stream("POST", "/v1/chat/completions",
                           json=chat_body("sse-tap", stream=True, _smoke_tools=True),
                           headers=HDR2) as sr:
            expect(sr.status_code == 200, "sse-tap 应200")
            text2 = "".join(ln + "\n" for ln in sr.iter_lines())
        expect("[DONE]" in text2 and '"usage"' in text2 and '"call_1"' in text2,
               "SSE 应包含 usage 尾片与工具调用片")
        sent = RECEIVED[-1]
        expect(sent.get("stream_options") == {"include_usage": True}, "应注入 include_usage")
        tap_row = fetch_logs(client)["rows"][0]
        expect(tap_row["prompt_tokens"] == 123 and tap_row["completion_tokens"] == 45
               and tap_row["cached_tokens"] == 7, "采集的usage/缓存应入库")
        expect(tap_row["tool_calls"] == 1, "流式工具调用次数应计1，实际 "
               + str(tap_row["tool_calls"]))
        expect(tap_row["client_name"] == "同事B", "归属应为同事B")
        add_row(tools=1, client="同事B")
        ok("流式采集：usage(123/45/缓存7)+工具调用次数(1) 全部入表；SSE 原样")

        # ---- 9. 非流式工具调用次数（一次2个工具） --------------------------
        r = client.post("/v1/chat/completions",
                        json=chat_body("mock-basic", _smoke_tools=True), headers=HDR)
        expect(r.status_code == 200, "_smoke_tools 请求应200")
        ns_row = fetch_logs(client)["rows"][0]
        expect(ns_row["tool_calls"] == 2, "非流式两个工具调用应计2，实际 "
               + str(ns_row["tool_calls"]))
        add_row(tools=2)
        ok("调用次数：非流式 message.tool_counts=2 正确入库")

        # ---- 10. 外部编辑 YAML 热加载 -------------------------------------
        doc2 = dict(doc)
        doc2["upstreams"] = list(doc["upstreams"]) + [
            {"alias": "late-add", "real_model": "mock-real",
             "api_base": "http://127.0.0.1:%d/v1" % MOCK_PORT,
             "api_key": "mk-late", "tag": "acct-late"}
        ]
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc2, f, allow_unicode=True, sort_keys=False)
        got = False
        for _ in range(24):
            time.sleep(0.25)
            rr = client.post("/v1/chat/completions", json=chat_body("late-add"), headers=HDR)
            if rr.status_code == 200:
                got = True
                break
        expect(got, "外部修改后新渠道未热加载")
        add_row()
        ok("热加载：手工改 YAML 后新渠道即时生效")

        # ---- 11. 导出 / 导入往返 ------------------------------------------
        exp = client.get("/admin/export").text
        expect(VKEY in exp and "late-add" in exp and "同事B" in exp, "导出缺内容")
        imp = client.post("/admin/import", json={"content": exp}).json()
        expect(imp["upstreams"] == 5 and imp["api_keys"] == 3, "导入数量不符: " + str(imp))
        ok("导入导出：YAML 往返一致（上游5/Keys3）")

        # ---- 12. 聚合校验（请求数 / 按使用者 / 工具合计） -------------------
        sm = client.get("/admin/stats/summary").json()
        g = sm["grand"]
        expect(g["requests"] == acc["rows"], "请求数聚合 %s != %s" % (g["requests"], acc["rows"]))
        expect(g["total"] == acc["tokens"], "token合计 %s != %s" % (g["total"], acc["tokens"]))
        expect(g["tools"] == acc["tools"], "工具合计 %s != %s" % (g["tools"], acc["tools"]))
        byc = {x["key"]: x for x in sm["by_client"]}
        n_main = sum(1 for c in ["主账号"] * 99 if c)  # placeholder avoided below
        main_cnt = byc["主账号"]["requests"]
        second_cnt = byc["同事B"]["requests"]
        expect(main_cnt + second_cnt == acc["rows"], "按使用者分组的请求数加总不符")
        tag_c = {x["key"]: x for x in sm["by_tag"]}
        expect(tag_c["acct-c"]["prompt"] == 123, "按tag聚合数值不符")
        ok("聚合：总请求数=%d 工具=%d | 按使用者 主账号%d+同事B%d"
           % (g["requests"], g["tools"], main_cnt, second_cnt))

        # ---- 13. Keys API 往返 & 清空日志 ---------------------------------
        nk = client.post("/admin/api_keys",
                         json={"name": "新人丙"}).json()  # 不带 key -> 自动生成
        expect(nk["key"].startswith("sk-virtual-"), "应自动生成Key值")
        dup = client.post("/admin/api_keys", json={"name": "新人丙"})
        expect(dup.status_code == 409, "重名应409")
        dl = client.delete("/admin/api_keys/" + nk["id"])
        expect(dl.status_code == 200, "删除Key失败")
        cl = client.delete("/admin/stats/logs").json()
        expect(cl["cleared"] == acc["rows"], "清空数量应为 " + str(acc["rows"]))
        expect(fetch_logs(client)["total"] == 0, "清空后应为0行")
        ok("Keys CRUD（自动生成/重名409/删除） + 清空日志生效")

        client.close()
        print("\n[smoke] 全部用例通过 ✔ （入库并清空 %d 行）" % acc["rows"])
        return 0
    finally:
        try:
            child.terminate()
            child.wait(timeout=5)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass
        try:
            mock_server.should_exit = True
            time.sleep(0.3)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
