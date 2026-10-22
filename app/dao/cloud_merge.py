"""DAO 云同步合并层：全量行导出 + 把云端行合并写进本地库。

依赖基础层 ``schema``（连接/锁/列常量/通用工具）。被 ``supabase_client`` 与
远程 VPS 的同步路径调用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .schema import (
    _GALLERY_COLUMNS, _GALLERY_CONTENT_COLUMNS, _GALLERY_INT_COLUMNS, _GALLERY_MARK_COLUMNS,
    _WRITE_LOCK, _optional_int, connect,
)


def all_gallery_rows(path: str | Path | None = None) -> list[dict]:
    conn = connect(path)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM galleries").fetchall()]
    finally:
        conn.close()


def all_run_rows(path: str | Path | None = None) -> list[dict]:
    conn = connect(path)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM scrape_runs").fetchall()]
    finally:
        conn.close()


def row_updated_at(row: dict, table: str) -> str:
    """同步用的「最后修改时间」：作品用 last_seen，批次用 finished_at/started_at。"""
    if table == "galleries":
        return str(row.get("last_seen") or "")
    return str(row.get("finished_at") or row.get("started_at") or "")


def upsert_gallery_rows_from_cloud(rows: Iterable[dict], path: str | Path | None = None) -> int:
    """把云端作品行合并进本地（需求：标记字段 1 覆盖 0）。

    - 新 gallery_id：直接 INSERT。
    - 已存在：标记字段（want_download/downloaded/hidden/site_favorite）
      ``max(本地, 云端)`` 合并 —— 任一方为 1 则结果 1（done 覆盖 todo，
      逻辑删除覆盖未删除），不受 last_seen 影响。
    - 内容字段：仅当 ``excluded.last_seen >= galleries.last_seen`` 时覆盖。
    - first_seen 取较旧、last_seen 取较新。

    返回本次处理的云端行数。
    """
    items = [r for r in rows if _optional_int(r.get("gallery_id"))]
    if not items:
        return 0
    cols = ", ".join(_GALLERY_COLUMNS)
    marks = ", ".join("?" for _ in _GALLERY_COLUMNS)
    mark_set = ", ".join(
        f"{c} = max(galleries.{c}, excluded.{c})" for c in _GALLERY_MARK_COLUMNS)
    content_set = ", ".join(
        f"{c} = CASE WHEN excluded.last_seen >= galleries.last_seen "
        f"THEN excluded.{c} ELSE galleries.{c} END"
        for c in _GALLERY_CONTENT_COLUMNS)
    time_set = (
        "first_seen = CASE WHEN excluded.first_seen < galleries.first_seen "
        "THEN excluded.first_seen ELSE galleries.first_seen END, "
        "last_seen = CASE WHEN excluded.last_seen >= galleries.last_seen "
        "THEN excluded.last_seen ELSE galleries.last_seen END")
    updates = ", ".join(p for p in (mark_set, content_set, time_set) if p)
    sql = (
        f"INSERT INTO galleries ({cols}) VALUES ({marks}) "
        f"ON CONFLICT(gallery_id) DO UPDATE SET {updates}"
    )
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.executemany(sql, [
                tuple(_optional_int(r.get(c)) if c in _GALLERY_INT_COLUMNS else r.get(c)
                      for c in _GALLERY_COLUMNS)
                for r in items
            ])
            conn.commit()
        finally:
            conn.close()
    return len(items)


def upsert_run_rows_from_cloud(rows: Iterable[dict], path: str | Path | None = None) -> int:
    """把云端批次行合并进本地：按 url 归并，时间较新者胜，返回改动行数。

    `scrape_runs` 没有 url 唯一约束（历史库中同一地址可能残留多条批次），
    所以这里逐条比对而不是依赖 ON CONFLICT。云端行可携带 run_id：
    本地还没有该 url 时按原 run_id 落库，保证 galleries.batch_id 的关联仍然成立；
    若该 run_id 已被别的地址占用，则改用自增 id，并把指向旧 id 的作品改指过来。
    """
    items = [r for r in rows if str(r.get("url") or "").strip()]
    if not items:
        return 0
    changed = 0
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            for r in items:
                url = str(r.get("url") or "").strip()
                cloud_ts = str(r.get("finished_at") or r.get("started_at") or "")
                row = conn.execute(
                    "SELECT run_id, started_at, finished_at FROM scrape_runs "
                    "WHERE url = ? ORDER BY run_id DESC LIMIT 1",
                    (url,),
                ).fetchone()
                values = (
                    str(r.get("status") or ""),
                    str(r.get("started_at") or ""),
                    str(r.get("finished_at") or "") or None,
                    int(r.get("pages_total") or 0),
                    int(r.get("pages_done") or 0),
                    int(r.get("items_seen") or 0),
                    int(r.get("items_new") or 0),
                    int(r.get("items_dup") or 0),
                    r.get("error"),
                )
                if row is None:
                    run_id = _optional_int(r.get("run_id"))
                    remap_from = run_id
                    if run_id is not None and conn.execute(
                        "SELECT 1 FROM scrape_runs WHERE run_id = ?", (run_id,)
                    ).fetchone():
                        run_id = None
                    cur = conn.execute(
                        "INSERT INTO scrape_runs (run_id, url, status, started_at, finished_at,"
                        " pages_total, pages_done, items_seen, items_new, items_dup, error)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, url) + values,
                    )
                    if run_id is None and remap_from is not None:
                        # 原 run_id 被占用：把引用它的作品改指到新插入的批次
                        conn.execute("UPDATE galleries SET batch_id = ? WHERE batch_id = ?",
                                     (int(cur.lastrowid), int(remap_from)))
                    changed += 1
                else:
                    local_ts = str(row["finished_at"] or row["started_at"] or "")
                    if cloud_ts and cloud_ts > local_ts:
                        conn.execute(
                            "UPDATE scrape_runs SET status = ?, started_at = ?, finished_at = ?,"
                            " pages_total = ?, pages_done = ?, items_seen = ?, items_new = ?,"
                            " items_dup = ?, error = ? WHERE run_id = ?",
                            values + (int(row["run_id"]),),
                        )
                        changed += 1
            conn.commit()
        finally:
            conn.close()
    return changed
