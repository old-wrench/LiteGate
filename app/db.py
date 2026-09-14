# -*- coding: utf-8 -*-
"""SQLite 统计层。

logs 表按请求记一行用量：
- 非流式：记录上游真实返回的 usage（输入/输出/缓存）；
- 流式  ：渠道开启 parse_stream_usage 时旁路扫描末片 usage，否则 token 记 0
          并置 is_stream=1（看板显示「流式无usage」标记）；
- client_name ：命中的分发虚拟Key名称（区分同事）；
- tool_calls  ：该次响应中模型发起的工具调用个数（开启采集时才统计）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    create_time       REAL    NOT NULL,             -- 请求完成时间戳(Unix秒)
    alias             TEXT    NOT NULL,             -- 客户端使用的模型别名
    real_model        TEXT    NOT NULL DEFAULT '',  -- 服务商真实模型名
    upstream_tag      TEXT    NOT NULL DEFAULT '',  -- 上游自定义标签(区分CodingPlan账号)
    client_name       TEXT    NOT NULL DEFAULT '',  -- 命中的分发Key名称(同事)
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,   -- 输入中命中上游缓存的token(<=prompt)
    tool_calls        INTEGER NOT NULL DEFAULT 0,   -- 本次响应工具调用次数
    is_stream         INTEGER NOT NULL DEFAULT 0,   -- 1=流式请求
    first_token_ms    INTEGER,                      -- 首Token延迟(TTFT,毫秒)；非流式/旧数据为NULL
    duration_ms       INTEGER,                      -- 请求进入网关到响应读完的总耗时(毫秒)
    cost              REAL                          -- 估算成本(元)，未维护价格为NULL
);
"""

#: 索引必须在 _migrate() 补齐新列之后创建（旧库升级场景列才存在）
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_logs_time   ON logs(create_time);
CREATE INDEX IF NOT EXISTS idx_logs_alias  ON logs(alias);
CREATE INDEX IF NOT EXISTS idx_logs_tag    ON logs(upstream_tag);
CREATE INDEX IF NOT EXISTS idx_logs_client ON logs(client_name);
"""

#: 聚合维度白名单：URL 参数 -> 列名（防注入）
_GROUP_COLUMNS = {
    "client": "client_name",
    "tag": "upstream_tag",
    "alias": "alias",
    "real_model": "real_model",
}


class StatsDB:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.executescript(_INDEXES)
            self._conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：给旧版本创建的库补齐新增列，历史数据不丢。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(logs)")}
        if "cached_tokens" not in cols:
            self._conn.execute(
                "ALTER TABLE logs ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0")
        if "client_name" not in cols:
            self._conn.execute(
                "ALTER TABLE logs ADD COLUMN client_name TEXT NOT NULL DEFAULT ''")
        if "tool_calls" not in cols:
            self._conn.execute(
                "ALTER TABLE logs ADD COLUMN tool_calls INTEGER NOT NULL DEFAULT 0")
        if "cost" not in cols:
            self._conn.execute("ALTER TABLE logs ADD COLUMN cost REAL")
        if "first_token_ms" not in cols:
            self._conn.execute("ALTER TABLE logs ADD COLUMN first_token_ms INTEGER")
        if "duration_ms" not in cols:
            self._conn.execute("ALTER TABLE logs ADD COLUMN duration_ms INTEGER")

    # ------------------------------------------------------------------
    def insert(
        self,
        *,
        create_time: float,
        alias: str,
        real_model: str,
        upstream_tag: str,
        client_name: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        tool_calls: int = 0,
        is_stream: bool = False,
        first_token_ms: Optional[int] = None,
        duration_ms: Optional[int] = None,
        cost: Optional[float] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs(create_time, alias, real_model, upstream_tag,"
                " client_name, prompt_tokens, completion_tokens, cached_tokens,"
                " tool_calls, is_stream, first_token_ms, duration_ms, cost)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    float(create_time),
                    str(alias),
                    str(real_model),
                    str(upstream_tag),
                    str(client_name or ""),
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    int(cached_tokens or 0),
                    int(tool_calls or 0),
                    1 if is_stream else 0,
                    None if first_token_ms is None else int(first_token_ms),
                    None if duration_ms is None else int(duration_ms),
                    None if cost is None else float(cost),
                ),
            )
            self._conn.commit()

    @staticmethod
    def _where(
        start: Optional[float],
        end: Optional[float],
        tag: Optional[str],
        alias: Optional[str],
        client: Optional[str] = None,
    ):
        conds, args = [], []
        if start is not None:
            conds.append("create_time >= ?")
            args.append(float(start))
        if end is not None:
            conds.append("create_time <= ?")
            args.append(float(end))
        if tag:
            conds.append("upstream_tag = ?")
            args.append(tag)
        if alias:
            conds.append("alias = ?")
            args.append(alias)
        if client:
            conds.append("client_name = ?")
            args.append(client)
        clause = (" WHERE " + " AND ".join(conds)) if conds else ""
        return clause, args

    # ------------------------------------------------------------------
    def query(
        self,
        start: Optional[float],
        end: Optional[float],
        tag: Optional[str],
        alias: Optional[str],
        limit: int,
        offset: int,
        client: Optional[str] = None,
    ):
        clause, args = self._where(start, end, tag, alias, client)
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM logs" + clause, args
            ).fetchone()[0]
            rows = self._conn.execute(
                "SELECT * FROM logs" + clause
                + " ORDER BY create_time DESC, id DESC LIMIT ? OFFSET ?",
                (*args, int(limit), int(offset)),
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def summary(
        self,
        start: Optional[float],
        end: Optional[float],
        tag: Optional[str],
        alias: Optional[str],
        client: Optional[str] = None,
    ) -> dict:
        """按 使用者/Tag/别名/真实模型 四个维度聚合，另给总计。

        每组同时给出：请求数(requests)、工具调用次数(tools)、
        输入(prompt)、输出(completion)、缓存(cached)、合计(total)；
        ttft 为平均首Token延迟(ms)，tps 为按生成时长加权的输出速度(tok/s)。
        """
        clause, args = self._where(start, end, tag, alias, client)
        out = {k: [] for k in ("by_client", "by_tag", "by_alias", "by_real_model")}
        out["grand"] = {}
        # 速度 = SUM(输出token) / SUM(生成时长)。流式扣除TTFT；非流式没有TTFT，用总耗时估算。
        base_cols = (
            " COUNT(*)                              AS req,"
            " COALESCE(SUM(tool_calls),0)           AS tc,"
            " COALESCE(SUM(prompt_tokens),0)        AS pt,"
            " COALESCE(SUM(completion_tokens),0)    AS ct,"
            " COALESCE(SUM(cached_tokens),0)        AS cc,"
            " COALESCE(SUM(prompt_tokens + completion_tokens),0) AS tt,"
            " AVG(first_token_ms)                   AS ttft,"
            " SUM(CASE WHEN duration_ms IS NULL OR completion_tokens <= 0 THEN 0"
            "     ELSE completion_tokens END) * 1000.0 /"
            " NULLIF(SUM(CASE WHEN duration_ms IS NULL OR completion_tokens <= 0 THEN 0"
            "     ELSE MAX(COALESCE(duration_ms,0) - COALESCE(first_token_ms,0),0) END),0)"
            "                                      AS tps,"
            " SUM(cost)                             AS cost"  # 全为NULL时保持NULL
        )
        with self._lock:
            for key, col in _GROUP_COLUMNS.items():
                rows = self._conn.execute(
                    "SELECT " + col + " AS k," + base_cols
                    + " FROM logs" + clause + " GROUP BY " + col + " ORDER BY tt DESC",
                    args,
                ).fetchall()
                out["by_" + key] = [
                    {"key": r["k"], "requests": r["req"], "tools": r["tc"],
                     "prompt": r["pt"], "completion": r["ct"], "cached": r["cc"],
                     "total": r["tt"], "ttft": r["ttft"], "tps": r["tps"],
                     "cost": r["cost"]}
                    for r in rows
                ]
            g = self._conn.execute(
                "SELECT" + base_cols + " FROM logs" + clause,
                args,
            ).fetchone()
        out["grand"] = {
            "requests": g[0], "tools": g[1], "prompt": g[2],
            "completion": g[3], "cached": g[4], "total": g[5],
            "ttft": g[6], "tps": g[7], "cost": g[8],
        }
        return out

    def distinct(self, column: str) -> list:
        col = _GROUP_COLUMNS.get(column)
        if col is None:
            raise ValueError("unsupported column: " + column)
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT " + col + " AS v FROM logs WHERE " + col + " <> '' ORDER BY v"
            ).fetchall()
        return [r["v"] for r in rows]

    def clear(self) -> int:
        """清空全部统计日志（谨慎操作），返回删除行数。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM logs")
            n = cur.rowcount
            self._conn.commit()
            self._conn.execute("VACUUM")
            self._conn.commit()
        return int(n or 0)

    def purge_before(self, cut_ts: float) -> int:
        """删除 create_time 早于 cut_ts 的记录（保留天数自动清理），返回删除行数。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM logs WHERE create_time < ?", (float(cut_ts),)
            )
            n = cur.rowcount
            self._conn.commit()
        return int(n or 0)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
