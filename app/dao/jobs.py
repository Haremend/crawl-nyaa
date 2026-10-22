"""DAO 下载任务层：download_jobs 表的入队/取消/恢复与状态更新。

依赖基础层 ``schema``（连接/锁/通用工具/utc_now）。
"""
from __future__ import annotations

from pathlib import Path

from .schema import _WRITE_LOCK, _optional_int, connect, utc_now


def create_job(gallery_id: int, *, title: str = "", fmt: str = "zip",
               source: str = "manual", batch_id: int | None = None,
               username: str = "", origin: str = "",
               path: str | Path | None = None) -> int:
    """把一个下载任务放进队列，返回 job_id。

    同一个作品 + 同一个格式，如果已经在队列里（queued/running），直接复用，避免重复排队。

    任务行里保存 username / origin，worker 用它们找到该用户的库与站点地址；
    API Key 不落库，只在队列进程内存里跟着任务走。
    """
    gid = _optional_int(gallery_id) or 0
    if gid <= 0:
        raise ValueError("无效的 gallery_id")
    now = utc_now()
    name = (username or "").strip()
    org = (origin or "").strip()
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            existing = conn.execute(
                "SELECT job_id FROM download_jobs WHERE gallery_id = ? AND format = ? "
                "AND status IN ('queued', 'running') ORDER BY job_id LIMIT 1",
                (gid, fmt),
            ).fetchone()
            if existing:
                return int(existing[0])
            cursor = conn.execute(
                "INSERT INTO download_jobs (username, origin, gallery_id, title, format, "
                "source, batch_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (name, org, gid, title or "", fmt, source, batch_id, now),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()


def update_job(job_id: int, path: str | Path | None = None, **fields) -> None:
    """更新任务字段（状态、进度、错误等）。"""
    allowed = {"status", "total_bytes", "done_bytes", "file_path", "error",
               "started_at", "finished_at", "title"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [int(job_id)]
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.execute(f"UPDATE download_jobs SET {assignments} WHERE job_id = ?", values)
            conn.commit()
        finally:
            conn.close()


def get_job(job_id: int, path: str | Path | None = None) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM download_jobs WHERE job_id = ?", (int(job_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_jobs(path: str | Path | None = None, limit: int = 100) -> list[dict]:
    """列出下载任务：正在跑/排队在前，其余按时间倒序。"""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM download_jobs "
            "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
            "job_id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [dict(r) for r in rows]
    finally:
        conn.close()


def claim_next_job(path: str | Path | None = None) -> dict | None:
    """原子地取出一个排队任务并标记为 running（供 worker 调用）。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            row = conn.execute(
                "SELECT * FROM download_jobs WHERE status = 'queued' ORDER BY job_id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE download_jobs SET status = 'running', started_at = ? WHERE job_id = ?",
                (utc_now(), int(row["job_id"])),
            )
            conn.commit()
            claimed = dict(row)
            claimed["status"] = "running"
            return claimed
        finally:
            conn.close()


def cancel_job(job_id: int, path: str | Path | None = None) -> bool:
    """取消排队中的任务；已在跑的任务交给 worker 自行收尾。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cursor = conn.execute(
                "UPDATE download_jobs SET status = 'cancelled', finished_at = ? "
                "WHERE job_id = ? AND status = 'queued'",
                (utc_now(), int(job_id)),
            )
            conn.commit()
            return bool(cursor.rowcount)
        finally:
            conn.close()


def cancel_queued_jobs(path: str | Path | None = None) -> int:
    """取消所有排队中的任务（不影响正在下载的）。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cursor = conn.execute(
                "UPDATE download_jobs SET status = 'cancelled', finished_at = ? "
                "WHERE status = 'queued'",
                (utc_now(),),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()


def reset_stale_jobs(path: str | Path | None = None) -> int:
    """进程重启后，把上次遗留的 running 任务重新放回队列。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cursor = conn.execute(
                "UPDATE download_jobs SET status = 'queued', started_at = NULL, done_bytes = 0 "
                "WHERE status = 'running'"
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()
