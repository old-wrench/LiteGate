# -*- coding: utf-8 -*-
"""网关核心：POST /v1/chat/completions 的鉴权、别名路由、参数合并与透传。

参数优先级（与需求文档一致）：
1. force_override_client_params = true
      -> 无论客户端传什么，一律用 Web 面板预设值替换；
2. 开关关闭时
      -> 客户端传了该参数就用客户端的；客户端没传、且面板预设了值，才注入预设值；
3. 面板预设为空 -> 网关完全不干预，该参数原样透传。

多虚拟Key鉴权：面板里配置 api_keys 列表（发给不同同事），请求 Bearer 命中哪把，
该次统计就归属到对应使用者；停用的Key与未知Key一律 401。

流式处理：SSE 字节级直通。渠道开启 parse_stream_usage 时走「旁路观察」——
注入 stream_options.include_usage，并在直通的同时逐行扫描末片 usage 与
tool_calls 片段提取统计值，绝不改动转发内容。
"""

from __future__ import annotations

import hmac
import json
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


class Gateway:
    #: 可在网关侧预设/覆盖的请求体参数
    PARAM_KEYS = ("max_tokens", "max_context_tokens")

    def __init__(self, store, db):
        self.store = store
        self.db = db
        # 上游模型生成可能很慢（长思考）：读超时放宽到 15 分钟
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=900.0, write=60.0, pool=30.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error"}, "code": status},
        status_code=status,
    )


def _extract_usage(usage: dict):
    """从 OpenAI 风格 usage 取 (输入, 输出, 缓存命中)。

    缓存兼容两种形态：
    - prompt_tokens_details.cached_tokens（OpenAI / 智谱 / Qwen 等）
    - 顶层 prompt_cache_hit_tokens（DeepSeek 风格）
    """
    try:
        pt = int(usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        pt = 0
    try:
        ct = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        ct = 0
    details = usage.get("prompt_tokens_details")
    raw_cached = details.get("cached_tokens") if isinstance(details, dict) else None
    try:
        cached = int(raw_cached or 0)
    except (TypeError, ValueError):
        cached = 0
    if not cached:
        try:
            cached = int(usage.get("prompt_cache_hit_tokens") or 0)
        except (TypeError, ValueError):
            cached = 0
    return pt, ct, cached


def _take_tool_calls(entries, rec: dict) -> None:
    """登记一次非流式 message.tool_calls 数组的出现。"""
    if isinstance(entries, list) and entries:
        rec["_tc_count"] = max(int(rec.get("_tc_count") or 0), len(entries))


def _scan_sse_line(raw: bytes, rec: dict) -> None:
    """从一行 SSE 数据中「旁路」提取统计信息 —— 提取不到就静默忽略。

    严格遵守透传原则：本函数只读分析，绝不改动任何转发给客户端的字节；
    仅当渠道开启 parse_stream_usage 时被调用。
    """
    if b"data" not in raw:
        return
    line = raw.decode("utf-8", "ignore").strip()
    if not line.startswith("data:") or "[DONE]" in line:
        return
    payload = line[5:].strip()
    if not payload.startswith("{"):
        return
    try:
        obj = json.loads(payload)
    except Exception:
        return  # 非 JSON 行：与提取无关
    if not isinstance(obj, dict):
        return
    usage = obj.get("usage")
    if isinstance(usage, dict):
        pt, ct, cached = _extract_usage(usage)
        # SSE 约定：最后一片携带累计值；重复更新即为最终值
        if pt:
            rec["prompt_tokens"] = pt
        if ct:
            rec["completion_tokens"] = ct
        if cached:
            rec["cached_tokens"] = cached
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        if c0.get("finish_reason") == "tool_calls":
            rec["_tc_any"] = True
        delta = c0.get("delta") or {}
        tcs = delta.get("tool_calls")
        if isinstance(tcs, list):
            seen = rec.setdefault("_tc_idx", set())
            for t in tcs:
                if isinstance(t, dict):
                    idx = t.get("index")
                    seen.add(idx if idx is not None else id(t))


def _finalize_tool_count(rec: dict) -> int:
    """把旁路收集的临时标记折算成最终工具调用次数，并清理临时键。"""
    idx = rec.pop("_tc_idx", None)
    any_flag = rec.pop("_tc_any", False)
    n = len(idx) if idx else 0
    if n == 0 and any_flag:
        n = 1
    return n


def _count_tool_calls_nonstream(data) -> int:
    """非流式：choices[0].message.tool_calls 数组长度；仅有 finish_reason 时记 1。"""
    if not isinstance(data, dict):
        return 0
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return 0
    c0 = choices[0] if isinstance(choices[0], dict) else {}
    msg = c0.get("message") or {}
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list) and tcs:
        return len(tcs)
    return 1 if c0.get("finish_reason") == "tool_calls" else 0


def _estimate_cost(rec: dict, up: dict) -> Optional[float]:
    """按渠道维护的单价（元/百万Tokens）折算本次请求成本。

    口径与厂商一致：缓存命中的输入按缓存价计，其余输入按输入价计；
    缓存价未维护时，缓存部分按输入价估算（保守、不失真）。
    三项全未维护、或本次没有任何 usage（流式未采集）时返回 None。
    """
    pi, po, pc = up.get("price_input"), up.get("price_output"), up.get("price_cache")
    if pi is None and po is None:
        return None
    p = int(rec.get("prompt_tokens") or 0)
    c = int(rec.get("completion_tokens") or 0)
    if p == 0 and c == 0:
        return None
    cached = min(int(rec.get("cached_tokens") or 0), p)
    pc_eff = pi if pc is None else pc
    cost = ((p - cached) * (pi or 0.0) + cached * (pc_eff or 0.0)
            + c * (po or 0.0)) / 1e6
    return round(cost, 8)


def create_gateway_router(gateway: Gateway) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        cfg = gateway.store.snapshot()

        # ---- 1. 多虚拟Key鉴权：命中哪把就归属哪个使用者 ---------------
        provided = request.headers.get("authorization", "")
        client_name = ""
        for k in cfg.get("api_keys") or []:
            if not k.get("enabled"):
                continue
            matched = hmac.compare_digest(
                provided.encode("utf-8"), ("Bearer " + k["key"]).encode("utf-8")
            )
            if matched:
                client_name = k["name"]
                break
        if not client_name:
            if not [x for x in (cfg.get("api_keys") or []) if x.get("enabled")]:
                return _err(401, "网关尚未配置任何启用的虚拟API Key，请先在 Web 面板添加")
            return _err(
                401,
                "无效的虚拟API Key（不匹配任何已分发的Key，或对应Key已被停用）",
            )

        # ---- 2. 解析请求体 -------------------------------------------
        try:
            body = await request.json()
        except Exception:
            return _err(400, "请求体不是合法的 JSON")
        if not isinstance(body, dict):
            return _err(400, "请求体必须是 JSON 对象")

        alias = body.get("model")
        up = gateway.store.route(str(alias)) if alias else None
        if up is None:
            return _err(
                400,
                "未知模型别名：" + str(alias)
                + "（请在 Web 面板「上游渠道配置」中先添加该 alias）",
            )

        # ---- 3. 参数优先级合并（见模块 docstring） --------------------
        force = bool(up.get("force_override_client_params"))
        for key in Gateway.PARAM_KEYS:
            preset = up.get(key)
            if preset is None:
                continue  # 预设为空：网关不干预
            if force or body.get(key) is None:
                body[key] = preset

        # ---- 4. 别名 -> 服务商真实模型名 ------------------------------
        body["model"] = up["real_model"]

        url = up["api_base"].rstrip("/") + "/chat/completions"
        want_stream = bool(body.get("stream"))

        # ---- 4.5 流式用量采集：注入 include_usage（客户端自带则尊重之）--
        tap_usage = want_stream and bool(up.get("parse_stream_usage"))
        if tap_usage:
            so = body.get("stream_options")
            if not isinstance(so, dict):
                so = {}
            so["include_usage"] = True
            body["stream_options"] = so

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if want_stream else "application/json",
            # 避免 gzip 分块干扰 SSE 直通
            "Accept-Encoding": "identity",
            "User-Agent": "LiteGate/1.0",
        }
        if up.get("api_key"):
            headers["Authorization"] = "Bearer " + up["api_key"]

        req = gateway.client.build_request("POST", url, json=body, headers=headers)
        try:
            resp = await gateway.client.send(req, stream=want_stream)
        except httpx.HTTPError as exc:
            # 上游连不上/超时：不做重试降级（明确排除项），仅返回 502
            return JSONResponse(
                {
                    "error": {
                        "message": "上游连接失败（未重试）：" + exc.__class__.__name__,
                        "type": "upstream_error",
                    }
                },
                status_code=502,
            )

        # ---- 5. 统计入库记录 -----------------------------------------
        rec = {
            "create_time": time.time(),
            "alias": str(alias),
            "real_model": up["real_model"],
            "upstream_tag": up.get("tag") or "",
            "client_name": client_name,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "tool_calls": 0,
            "is_stream": 1 if want_stream else 0,
            "cost": None,
        }

        async def _insert_safe() -> None:
            """入库失败只记日志，绝不影响正在进行的响应。"""
            try:
                # 成本按请求发生时刻的渠道单价折算，入库后不随改价波动
                rec["cost"] = _estimate_cost(rec, up)
                gateway.db.insert(**rec)
            except Exception as exc:
                print("[gateway] 用量入库失败：" + str(exc), flush=True)

        try:
            # ---- 6. 上游 4xx/5xx 错误：原样透传，不包装 --------------
            if resp.status_code >= 400:
                payload = await resp.aread()
                media_type = resp.headers.get("content-type", "application/json")
                status = resp.status_code
                await resp.aclose()
                # 失败请求没有 usage，不入库，避免污染统计
                return Response(payload, status_code=status, media_type=media_type)

            if want_stream:
                media_type = resp.headers.get("content-type", "text/event-stream")

                async def relay():
                    """纯直通：结束后按约定记 0，看板打「流式无usage」标记。"""
                    try:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                    finally:
                        await resp.aclose()
                        rec["tool_calls"] = 0
                        await _insert_safe()

                async def relay_tap():
                    """旁路观察直通：字节原样转发（先 yield 后扫描，零额外延迟），
                    同时逐行扫描 SSE 提取 usage / tool_calls 入库。"""
                    buf = b""
                    try:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                            buf += chunk
                            while True:  # 只处理完整行，残行留到下一轮
                                nl = buf.find(b"\n")
                                if nl < 0:
                                    break
                                line, buf = buf[:nl], buf[nl + 1:]
                                _scan_sse_line(line, rec)
                    finally:
                        if buf:
                            _scan_sse_line(buf, rec)
                        await resp.aclose()
                        rec["tool_calls"] = _finalize_tool_count(rec)
                        await _insert_safe()

                return StreamingResponse(
                    relay_tap() if tap_usage else relay(),
                    media_type=media_type,
                )

            # ---- 7. 非流式：读取上游 usage 后原样回传 ----------------
            payload = await resp.aread()
            media_type = resp.headers.get("content-type", "application/json")
            status = resp.status_code
            await resp.aclose()
            data = None
            try:
                data = json.loads(payload)
            except Exception:
                pass
            if isinstance(data, dict):
                usage = data.get("usage")
                if isinstance(usage, dict):
                    pt, ct, cached = _extract_usage(usage)
                    rec["prompt_tokens"], rec["completion_tokens"] = pt, ct
                    rec["cached_tokens"] = cached
                rec["tool_calls"] = _count_tool_calls_nonstream(data)
            await _insert_safe()
            return Response(payload, status_code=status, media_type=media_type)
        finally:
            # send(stream=False) 时需要手动释放连接资源
            if not want_stream and not resp.is_closed:
                await resp.aclose()

    # ---- GET /v1/models（及 /models 别名）：OpenAI 兼容模型列表 ----------
    # 实时读取当前配置快照：面板增删改上游后立即生效，无需重启。
    # 鉴权策略：携带 Bearer 时必须命中某把启用的虚拟Key（否则 401）；
    # 不携带则放行（模型别名列表非敏感，便于无鉴权客户端枚举模型）。
    @router.get("/v1/models")
    @router.get("/models")
    async def list_models(request: Request):
        cfg = gateway.store.snapshot()
        auth = request.headers.get("authorization", "").strip()
        if auth:
            matched = False
            for k in cfg.get("api_keys") or []:
                if not k.get("enabled"):
                    continue
                if hmac.compare_digest(
                    auth.encode("utf-8"), ("Bearer " + k["key"]).encode("utf-8")
                ):
                    matched = True
                    break
            if not matched:
                return _err(
                    401,
                    "无效的虚拟API Key（不匹配任何已分发的Key，或对应Key已被停用）",
                )
        created = int(time.time())
        data = []
        for u in cfg.get("upstreams") or []:
            entry = {
                # OpenAI 标准字段
                "id": u.get("alias") or "",
                "object": "model",
                "created": created,
                "owned_by": (u.get("tag") or "cc-router"),
                # 扩展字段：OpenAI SDK 会忽略未知字段，不影响标准客户端
                "real_model": u.get("real_model") or u.get("alias") or "",
            }
            # 上下文长度：面板配置了 max_context_tokens 才返回（OpenRouter 风格字段名）
            mx = u.get("max_context_tokens")
            if mx:
                entry["context_length"] = int(mx)
            data.append(entry)
        return JSONResponse({"object": "list", "data": data})

    return router
