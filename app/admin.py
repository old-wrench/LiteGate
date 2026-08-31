# -*- coding: utf-8 -*-
"""Web 管理面 API：分发Key管理、上游渠道 CRUD、导入导出 YAML、统计查询。

安全边界说明：管理面（/、/admin、/static）的访问来源由 admin_access 配置控制
（local=仅本机 / lan=本机+局域网 / allowlist=白名单 / any=不限制，
本机回环永远放行），由 app/server.py 的 admin_access_guard 中间件执行；
/v1 数据面始终由虚拟Key鉴权，不在该配置的管辖范围内。
"""

from __future__ import annotations

import os
import secrets
import shutil

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .config import (
    ConfigError,
    normalize_admin_access,
    normalize_doc,
    normalize_keys,
    normalize_upstream,
    parse_listen,
)

api = APIRouter(prefix="/admin")


async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return data


def _store(request: Request):
    return request.app.state.store


def _db(request: Request):
    return request.app.state.db


def _float_param(request: Request, name: str):
    v = request.query_params.get(name)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        raise HTTPException(status_code=400, detail=name + " 需为 Unix 秒时间戳")


# ---------------------------------------------------------------------------
# 监听设置（Key 的管理走 /admin/api_keys）
# ---------------------------------------------------------------------------

@api.get("/config")
def get_config(request: Request):
    snap = _store(request).snapshot()
    # 展示兼容：老面板/脚本仍可读到虚拟Key字段，取第一把启用Key
    keys = [k for k in snap["api_keys"] if k.get("enabled")]
    return {
        "listen_addr": snap["listen_addr"],
        "admin_access": snap.get("admin_access") or {"mode": "lan", "allow": []},
        "primary_key": keys[0]["key"] if keys else "",
        "api_keys": snap["api_keys"],
        "upstreams": snap["upstreams"],
    }


@api.put("/settings")
async def put_settings(request: Request):
    body = await _json_body(request)
    patch: dict = {}
    if "listen_addr" in body:
        addr = str(body.get("listen_addr") or "").strip()
        try:
            parse_listen(addr)  # 仅做格式校验（127.0.0.1 / 0.0.0.0 / 内网IP / 主机名均可）
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        patch["listen_addr"] = addr
    if "admin_access" in body:
        try:
            patch["admin_access"] = normalize_admin_access(body.get("admin_access"))
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if not patch:
        raise HTTPException(status_code=400, detail="没有可更新的设置项")

    fresh = _store(request).update(lambda d: {**d, **patch})
    return {
        "listen_addr": fresh["listen_addr"],
        "admin_access": fresh["admin_access"],
        "listen_requires_restart": True,
    }


# ---------------------------------------------------------------------------
# 分发虚拟Key（发给不同同事）CRUD
# ---------------------------------------------------------------------------

@api.get("/api_keys")
def list_keys(request: Request):
    return _store(request).snapshot()["api_keys"]


@api.post("/api_keys")
async def create_key(request: Request):
    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    key = str(body.get("key") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Key名称不能为空（如同事姓名/昵称）")
    if not key:
        key = "sk-virtual-" + secrets.token_urlsafe(18)
    item = {"id": "", "name": name, "key": key,
            "enabled": body.get("enabled", True) in (True, "true", "True", 1)}
    try:
        checked = normalize_keys([item])[0]
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    snap = _store(request).snapshot()
    names = {k["name"] for k in snap["api_keys"]}
    values = {k["key"] for k in snap["api_keys"]}
    if checked["name"] in names:
        raise HTTPException(status_code=409, detail="Key名称已存在：" + checked["name"])
    if checked["key"] in values:
        raise HTTPException(status_code=409, detail="该Key值已存在，请换一个")

    def mapper(d):
        d["api_keys"].append(checked)
        return d

    _store(request).update(mapper)
    return checked


def _find_key(snap: dict, kid: str):
    for k in snap["api_keys"]:
        if k["id"] == kid:
            return k
    return None


@api.put("/api_keys/{kid}")
async def update_key(kid: str, request: Request):
    body = await _json_body(request)
    store = _store(request)
    existing = _find_key(store.snapshot(), kid)
    if existing is None:
        raise HTTPException(status_code=404, detail="Key不存在")

    merged = dict(existing)
    merged.update(body)
    merged["id"] = kid
    try:
        checked_list = normalize_keys([merged])
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    checked = checked_list[0]

    snap = store.snapshot()
    clash = next(
        (k for k in snap["api_keys"]
         if k["id"] != kid and (k["key"] == checked["key"] or k["name"] == checked["name"])),
        None,
    )
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail="与其他Key冲突的字段：" + ("名称" if clash["name"] == checked["name"] else "值"),
        )

    def mapper(d):
        d["api_keys"] = [checked if k["id"] == kid else k for k in d["api_keys"]]
        return d

    store.update(mapper)
    return checked


@api.delete("/api_keys/{kid}")
async def delete_key(kid: str, request: Request):
    store = _store(request)
    if _find_key(store.snapshot(), kid) is None:
        raise HTTPException(status_code=404, detail="Key不存在")

    def mapper(d):
        d["api_keys"] = [k for k in d["api_keys"] if k["id"] != kid]
        return d

    fresh = store.update(mapper)
    remaining = len(fresh["api_keys"])
    warn_all_disabled = remaining == 0 or not any(
        k.get("enabled") for k in fresh["api_keys"]
    )
    return {"remaining": remaining, "warn_no_enabled_keys": warn_all_disabled}


# ---------------------------------------------------------------------------
# 上游渠道 CRUD
# ---------------------------------------------------------------------------

@api.get("/upstreams")
def list_upstreams(request: Request):
    return _store(request).snapshot()["upstreams"]


@api.post("/upstreams")
async def create_upstream(request: Request):
    body = await _json_body(request)
    try:
        item = normalize_upstream(body)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    snap = _store(request).snapshot()
    if any(u["alias"] == item["alias"] for u in snap["upstreams"]):
        raise HTTPException(status_code=409, detail="alias 已存在：" + item["alias"])

    def mapper(d):
        d["upstreams"].append(item)
        return d

    _store(request).update(mapper)
    return item


@api.put("/upstreams/{uid}")
async def update_upstream(uid: str, request: Request):
    body = await _json_body(request)
    snap = _store(request).snapshot()
    existing = next((u for u in snap["upstreams"] if u["id"] == uid), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="上游配置不存在")
    merged = dict(existing)
    merged.update(body)
    merged["id"] = uid  # 不允许通过编辑改 id
    try:
        item = normalize_upstream(merged)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    def mapper(d):
        d["upstreams"] = [item if u.get("id") == uid else u for u in d["upstreams"]]
        return d

    _store(request).update(mapper)
    return item


@api.delete("/upstreams/{uid}")
async def delete_upstream(uid: str, request: Request):
    snap = _store(request).snapshot()
    if not any(u["id"] == uid for u in snap["upstreams"]):
        raise HTTPException(status_code=404, detail="上游配置不存在")

    def mapper(d):
        d["upstreams"] = [u for u in d["upstreams"] if u["id"] != uid]
        return d

    fresh = _store(request).update(mapper)
    return {"remaining": len(fresh["upstreams"])}


# ---------------------------------------------------------------------------
# 导出 / 导入
# ---------------------------------------------------------------------------

@api.get("/export")
def export_config(request: Request):
    text = yaml.safe_dump(
        _store(request).snapshot(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return Response(
        content=text,
        media_type="application/x-yaml",
        headers={"Content-Disposition": 'attachment; filename="litegate-config.yaml"'},
    )


@api.post("/import")
async def import_config(request: Request):
    body = await _json_body(request)
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="缺少 YAML 文本 content")
    try:
        raw = yaml.safe_load(content)
        newdoc = normalize_doc(raw)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail="YAML 解析失败：" + str(exc))
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    store = _store(request)
    if os.path.exists(store.path):  # 导入前备份一份旧配置
        try:
            shutil.copy2(store.path, store.path + ".bak")
        except OSError:
            pass
    store.update(lambda d: newdoc)
    return {
        "upstreams": len(newdoc["upstreams"]),
        "api_keys": len(newdoc["api_keys"]),
        "backup_created": os.path.exists(store.path + ".bak"),
    }


# ---------------------------------------------------------------------------
# 统计看板
# ---------------------------------------------------------------------------

def _filters(request: Request):
    tag = request.query_params.get("tag") or None
    alias = request.query_params.get("alias") or None
    client = request.query_params.get("client") or None
    start = _float_param(request, "start")
    end = _float_param(request, "end")
    return start, end, tag, alias, client


@api.get("/stats/logs")
def stats_logs(request: Request):
    start, end, tag, alias, client = _filters(request)
    try:
        limit = int(request.query_params.get("limit", 200))
        offset = int(request.query_params.get("offset", 0))
    except ValueError:
        raise HTTPException(status_code=400, detail="limit/offset 需为整数")
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    rows, total = _db(request).query(start, end, tag, alias, limit, offset, client)
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


@api.get("/stats/summary")
def stats_summary(request: Request):
    start, end, tag, alias, client = _filters(request)
    return _db(request).summary(start, end, tag, alias, client)


@api.delete("/stats/logs")
async def clear_logs(request: Request):
    return {"cleared": _db(request).clear()}


@api.get("/meta")
def meta(request: Request):
    """下拉筛选候选：取 配置中的值 ∪ 数据库中出现过的值。"""
    snap = _store(request).snapshot()
    db = _db(request)
    tags = {(u.get("tag") or "") for u in snap["upstreams"]} | set(db.distinct("tag"))
    aliases = {u["alias"] for u in snap["upstreams"]} | set(db.distinct("alias"))
    clients = {k["name"] for k in snap["api_keys"]} | set(db.distinct("client"))
    return {
        "tags": sorted(t for t in tags if t),
        "aliases": sorted(a for a in aliases if a),
        "clients": sorted(c for c in clients if c),
    }
