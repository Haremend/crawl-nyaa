"""DAO 抓取队列层：scrape_jobs 表的持久化，支持重启恢复。

scrape_jobs 是「待抓取 URL 队列」的持久化（与 download_jobs 同级，每个用户库
一张）。start() 入队即 INSERT（status=queued）；worker 取出跑时标 running，
跑完标 done/error/cancelled；cancel() 把所有 queued 标 cancelled。重启时
``list_pending_scrape_jobs`` 读出 queued + 崩溃残留的 running（先 reset 成
queued），回填进内存 _queue，实现重启可恢复。

status 取值：queued / running / done / cancelled / error。
"""
from __future__ import annotations

import json
from pathlib import Path

from .schema import _WRITE_LOCK, connect, utc_now


def insert_scrape_job(url: str, options: dict, *, username: str = "",
                      path: str | Path | None = None) -> int:
    """入队一条抓取任务，返回 job_id。options 存为 JSON（max_pages/delay/...）。"""
    now = utc_now()
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cur = conn.execute(
                "INSERT INTO scrape_jobs (username, url, options, status, created_at) "
                "VALUES (?, ?, ?, 'queued', ?)",
                (str(username or ""), str(url), json.dumps(options, ensure_ascii=False), now),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def list_pending_scrape_jobs(path: str | Path | None = None) -> list[dict]:
    """取所有未完成的任务（queued + running），按入队顺序返回。

    running 是「上次进程崩溃时正在跑」的残留，调用方应先 reset_running 再读。
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT job_id, username, url, options, status, created_at, started_at "
            "FROM scrape_jobs WHERE status IN ('queued', 'running') "
            "ORDER BY job_id"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            opts = json.loads(str(row["options"])) if row["options"] else {}
        except (ValueError, TypeError):
            opts = {}
        out.append({
            "job_id": int(row["job_id"]),
            "username": str(row["username"] or ""),
            "url": str(row["url"]),
            "options": opts,
            "status": str(row["status"]),
            "created_at": str(row["created_at"] or ""),
            "started_at": str(row["started_at"] or "") or None,
        })
    return out


def update_scrape_job(job_id: int, *, status: str, started_at: str | None = None,
                      finished_at: str | None = None, error: str | None = None,
                      path: str | Path | None = None) -> None:
    """更新任务状态（running/done/cancelled/error）与时间戳。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.execute(
                "UPDATE scrape_jobs SET status = ?, started_at = COALESCE(?, started_at), "
                "finished_at = COALESCE(?, finished_at), error = ? WHERE job_id = ?",
                (str(status), started_at, finished_at, error, int(job_id)),
            )
            conn.commit()
        finally:
            conn.close()


def cancel_queued_scrape_jobs(path: str | Path | None = None) -> int:
    """把所有 queued 任务标 cancelled（cancel 按钮清空队列时调用）。返回受影响行数。"""
    now = utc_now()
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cur = conn.execute(
                "UPDATE scrape_jobs SET status = 'cancelled', finished_at = ? "
                "WHERE status = 'queued'",
                (now,),
            )
            conn.commit()
            return int(cur.rowcount)
        finally:
            conn.close()


def reset_running_scrape_jobs(path: str | Path | None = None) -> int:
    """把残留的 running（进程崩溃未跑完）重置回 queued，供重启后重新入队。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cur = conn.execute(
                "UPDATE scrape_jobs SET status = 'queued', started_at = NULL "
                "WHERE status = 'running'"
            )
            conn.commit()
            return int(cur.rowcount)
        finally:
            conn.close()


def cleanup_finished_scrape_jobs(path: str | Path | None = None,
                                 keep: int = 200) -> None:
    """清理已结束（done/cancelled/error）的旧任务，只保留最近 keep 条，防表膨胀。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            ids = [row[0] for row in conn.execute(
                "SELECT job_id FROM scrape_jobs WHERE status IN ('done', 'cancelled', 'error') "
                "ORDER BY job_id DESC LIMIT ?",
                (int(keep),)
            ).fetchall()]
            if not ids:
                return
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM scrape_jobs WHERE status IN ('done','cancelled','error') "
                f"AND job_id NOT IN ({placeholders})",
                ids,
            )
            conn.commit()
        finally:
            conn.close()
