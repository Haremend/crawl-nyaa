"""DAO 批次层：抓取批次（scrape_runs）记录与库统计。

依赖基础层 ``schema``（连接/锁/通用工具）与聚类层 ``clustering``（``stats`` 用
``_fetch_rows`` + ``_cluster_rows`` 算「去重后作品数」）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import _WRITE_LOCK, _chunks, connect, utc_now
from .clustering import _cluster_rows, _fetch_rows


def create_run(url: str, path: str | None = None) -> int:
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            cursor = conn.execute(
                "INSERT INTO scrape_runs (url, status, started_at) VALUES (?, 'running', ?)",
                (url, utc_now()),
            )
            conn.commit()
            return int(cursor.lastrowid)
    finally:
        conn.close()


def finish_run(
    run_id: int,
    *,
    status: str,
    pages_total: int = 0,
    pages_done: int = 0,
    items_seen: int = 0,
    items_new: int = 0,
    items_dup: int = 0,
    error: str | None = None,
    path: str | None = None,
) -> None:
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            conn.execute(
                """UPDATE scrape_runs SET status = ?, finished_at = ?, pages_total = ?,
                   pages_done = ?, items_seen = ?, items_new = ?, items_dup = ?, error = ?
                   WHERE run_id = ?""",
                (status, utc_now(), pages_total, pages_done, items_seen, items_new, items_dup, error, run_id),
            )
            conn.commit()
    finally:
        conn.close()


def last_run(path: str | None = None) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM scrape_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def latest_run_for_url(url: str, path: str | None = None) -> dict | None:
    """返回该列表地址最近一次成功抓取的批次记录（用于增量抓取判断）。"""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM scrape_runs WHERE url = ? AND status = 'done' "
            "ORDER BY run_id DESC LIMIT 1",
            (str(url),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_run(run_id: int, path: str | None = None) -> bool:
    """删除一次爬取记录（用于「爬取结果为 0 时不保留该批次号」）。"""
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            cursor = conn.execute("DELETE FROM scrape_runs WHERE run_id = ?", (int(run_id),))
            conn.commit()
            return bool(cursor.rowcount)
    finally:
        conn.close()


def merge_runs_for_url(url: str, keep_run_id: int,
                       path: str | None = None) -> int:
    """同一 URL 只保留最新一次批次。

    删除该 URL 的其它批次记录，并把旧批次的条目归属改到最新批次，
    便于「保存本批全部数据」拿到该地址的最新完整清单。返回清理的旧批次数量。
    """
    keep = int(keep_run_id or 0)
    if keep <= 0:
        return 0
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            old_ids = [int(row[0]) for row in conn.execute(
                "SELECT run_id FROM scrape_runs WHERE url = ? AND run_id <> ?",
                (str(url), keep),
            )]
            if not old_ids:
                return 0
            for chunk in _chunks(old_ids, 400):
                marks = ",".join("?" for _ in chunk)
                conn.execute(
                    f"UPDATE galleries SET batch_id = ? WHERE batch_id IN ({marks})",
                    [keep] + list(chunk),
                )
                conn.execute(f"DELETE FROM scrape_runs WHERE run_id IN ({marks})", list(chunk))
            conn.commit()
            return len(old_ids)
    finally:
        conn.close()


def reset_all(path: str | None = None) -> dict:
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            galleries = conn.execute("SELECT COUNT(*) FROM galleries").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]
            conn.execute("DELETE FROM galleries")
            conn.execute("DELETE FROM scrape_runs")
            conn.commit()
        return {"galleries": int(galleries), "runs": int(runs)}
    finally:
        conn.close()


def list_batches(path: str | None = None, limit: int = 100) -> list[dict]:
    """抓取批次列表：批次号、来源 URL、时间、条目数。"""
    conn = connect(path)
    try:
        runs = [dict(row) for row in conn.execute(
            "SELECT run_id, url, status, started_at, finished_at, pages_done, "
            "items_seen, items_new, items_dup FROM scrape_runs ORDER BY run_id DESC LIMIT ?",
            (max(1, int(limit)),),
        )]
        counts: dict[int, int] = {}
        for row in conn.execute(
            "SELECT batch_id, COUNT(*) AS n FROM galleries WHERE batch_id > 0 GROUP BY batch_id"
        ):
            counts[int(row[0])] = int(row[1])
    finally:
        conn.close()
    for run in runs:
        run["works"] = counts.get(int(run["run_id"]), 0)
    return runs


def stats(path: str | None = None) -> dict:
    rows = _fetch_rows(None, path)
    works = len(_cluster_rows(rows, True))
    details = sum(1 for row in rows if (row["detail_fetched_at"] or ""))
    covers = sum(1 for row in rows if (row["cover_file"] or ""))
    want = sum(1 for row in rows if (row["want_download"] or 0))
    done = sum(1 for row in rows if (row["downloaded"] or 0))
    site_fav = sum(1 for row in rows if (row["site_favorite"] or 0))
    conn = connect(path)
    try:
        hidden_count = int(conn.execute(
            "SELECT COUNT(*) FROM galleries WHERE hidden = 1").fetchone()[0])
    finally:
        conn.close()
    return {
        "entries": len(rows),
        "hidden": hidden_count,
        "works": works,
        "duplicates": max(0, len(rows) - works),
        "details": details,
        "covers": covers,
        "want_download": want,
        "downloaded": done,
        "site_favorites": site_fav,
        "last_run": last_run(path),
    }
