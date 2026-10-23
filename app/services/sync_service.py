"""sync 业务逻辑：本地 ↔ 云端（remote VPS 优先 / Supabase 直连）+ VPS 端点。

从 app/server.py 的 _handle_sync* / _handle_remote_account / _handle_remote_sync
抽出。门禁（登录校验 _ensure_login / Bearer _require_remote_guard）留在
controller；本层只做业务编排与响应组装。行为与拆分前完全一致。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from app import config, db, remote_client, supabase_client
from app.services._log import cloud_log


def remote_base_masked() -> str:
    """把 CLAW_REMOTE_BASE 脱敏成 host[:port]（不暴露路径/query）。"""
    base = config.REMOTE_BASE
    if not base:
        return ""
    netloc = urlsplit(base).netloc
    return netloc or base


def status() -> dict:
    """同步可用性状态（controller 负责 _json）。"""
    if config.storage_is_cloud():
        return {"ok": True, "configured": True, "available": True,
                "cloud_mode": True,
                "message": "云模式：数据直连云端，无需同步"}
    if config.remote_configured():
        return {"ok": True, "configured": True, "available": True,
                "cloud_mode": False, "via_remote": True,
                "remote_base_masked": remote_base_masked(),
                "message": "已通过远程 VPS 同步（本机无需 Supabase 凭据）。"}
    configured = supabase_client.configured()
    available = False
    if configured:
        try:
            available = supabase_client.tables_exist()
        except supabase_client.SupabaseError:
            available = False
    return {"ok": True, "configured": configured, "available": available,
            "cloud_mode": False, "via_remote": False,
            "remote_base_masked": remote_base_masked()}


def merge_unlogin_into(username: str) -> dict:
    """登录时把内置匿名账号 unlogin_user 的本地数据并入登录账号库（§12.3）。

    方向固定：unlogin_user → 登录账号（单向，不反向）；按现有合并规则
    （cloud_merge：标记列 1 覆盖 0、last_seen 新者胜）执行，全程复用
    db.upsert_gallery_rows_from_cloud / upsert_run_rows_from_cloud，不新增规则。
    unlogin_user 本地库保留原数据（幂等：重复登录时已存在的 gallery_id
    一律跳过，不会重复合并出脏数据）。

    返回 {galleries, runs} 为**真实新增**条数（合并前目标库中不存在的行），
    供登录响应回显「已并入 N 条匿名数据」；重复登录时新增数为 0。
    """
    unlogin_db = db.user_db_path(config.UNLOGIN_USER)
    target_db = db.user_db_path(username)
    db.init_db(unlogin_db)
    db.init_db(target_db)
    limit = config.SUPABASE_SYNC_MAX_ROWS

    def _new_count(existing_ids: set, new_ids: list) -> int:
        return sum(1 for gid in new_ids if gid not in existing_ids)

    gallery_rows = db.all_gallery_rows(unlogin_db)[:limit]
    run_rows = db.all_run_rows(unlogin_db)[:limit]

    def _existing(table: str, key: str, values: list) -> set:
        values = [v for v in values if v is not None]
        if not values:
            return set()
        conn = db.connect(target_db)
        try:
            placeholders = ",".join("?" for _ in values)
            rows = conn.execute(
                f"SELECT {key} FROM {table} WHERE {key} IN ({placeholders})",
                values).fetchall()
            return {row[0] for row in rows}
        finally:
            conn.close()

    existing_g = _existing("galleries", "gallery_id",
                           [r.get("gallery_id") for r in gallery_rows])
    new_g = _new_count(existing_g, [r.get("gallery_id") for r in gallery_rows])
    new_r = 0
    if run_rows:
        existing_r = _existing("scrape_runs", "url",
                               [r.get("url") for r in run_rows])
        new_r = sum(1 for r in run_rows if r.get("url") not in existing_r)

    db.upsert_gallery_rows_from_cloud(gallery_rows, target_db)
    db.upsert_run_rows_from_cloud(run_rows, target_db)
    return {"galleries": new_g, "runs": new_r}


def sync(user: dict) -> dict:
    """全量双向同步（拉 + 推）。user 为已解析的登录用户 dict。"""
    if config.storage_is_cloud():
        return {"ok": True, "cloud_mode": True,
                "message": "当前为云模式：数据直连云端，无需同步"}
    username = user["username"]
    user_db = db.user_db_path(username)
    if config.remote_configured():
        try:
            res = remote_client.get_client().pull(username)
            pulled_g = db.upsert_gallery_rows_from_cloud(res.get("galleries", []), user_db)
            pulled_r = db.upsert_run_rows_from_cloud(res.get("runs", []), user_db)
        except remote_client.RemoteError as exc:
            return {"ok": False, "error": f"远程 VPS 拉取失败：{exc}", "status": 502}
        res2 = remote_client.get_client().push(
            username, db.all_gallery_rows(user_db)[:config.SUPABASE_SYNC_MAX_ROWS],
            db.all_run_rows(user_db)[:config.SUPABASE_SYNC_MAX_ROWS])
        return {"ok": True, "username": username,
                "pulled": {"galleries": pulled_g, "runs": pulled_r},
                "pushed": res2.get("pushed", {}),
                "cloud": res.get("counts", {}),
                "local": {"galleries": len(db.all_gallery_rows(user_db)),
                           "runs": len(db.all_run_rows(user_db))},
                "via_remote": True}
    if not supabase_client.configured():
        return {"ok": False,
                "error": "未配置云端数据库：请在 .env 里填写 CLAW_SUPABASE_PROJECT_REF 与 CLAW_SUPABASE_TOKEN",
                "status": 400}
    try:
        result = supabase_client.sync_user(username, user_db)
    except supabase_client.SupabaseError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status or 502}
    return {"ok": True, **result}


def pull(user: dict) -> dict:
    """拉取云端 → 合并本地（1 覆盖 0 / last_seen 新者胜）。"""
    if config.storage_is_cloud():
        return {"ok": True, "cloud_mode": True,
                "message": "当前为云模式：数据直连云端，无需同步"}
    username = user["username"]
    user_db = db.user_db_path(username)
    if config.remote_configured():
        try:
            res = remote_client.get_client().pull(username)
        except remote_client.RemoteError as exc:
            return {"ok": False, "error": f"远程 VPS 拉取失败：{exc}", "status": 502}
        pulled_g = db.upsert_gallery_rows_from_cloud(res.get("galleries", []), user_db)
        pulled_r = db.upsert_run_rows_from_cloud(res.get("runs", []), user_db)
        return {"ok": True, "username": username,
                "cloud": res.get("counts", {}),
                "pulled": {"galleries": pulled_g, "runs": pulled_r},
                "local": {"galleries": len(db.all_gallery_rows(user_db)),
                           "runs": len(db.all_run_rows(user_db))},
                "via_remote": True}
    if not supabase_client.configured():
        return {"ok": False,
                "error": "未配置云端数据库：请在 .env 里填写 CLAW_SUPABASE_PROJECT_REF 与 CLAW_SUPABASE_TOKEN",
                "status": 400}
    try:
        result = supabase_client.pull_user(username, user_db)
    except supabase_client.SupabaseError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status or 502}
    return {"ok": True, **result}


def push(user: dict) -> dict:
    """推送本地 → 覆盖远端。"""
    if config.storage_is_cloud():
        return {"ok": True, "cloud_mode": True,
                "message": "当前为云模式：数据直连云端，无需同步"}
    username = user["username"]
    user_db = db.user_db_path(username)
    if config.remote_configured():
        limit = config.SUPABASE_SYNC_MAX_ROWS
        galleries = db.all_gallery_rows(user_db)[:limit]
        runs = db.all_run_rows(user_db)[:limit]
        try:
            res = remote_client.get_client().push(username, galleries, runs)
        except remote_client.RemoteError as exc:
            return {"ok": False, "error": f"远程 VPS 推送失败：{exc}", "status": 502}
        return {"ok": True, "username": username,
                "pushed": res.get("pushed", {}),
                "local": {"galleries": len(galleries), "runs": len(runs)},
                "via_remote": True}
    if not supabase_client.configured():
        return {"ok": False,
                "error": "未配置云端数据库：请在 .env 里填写 CLAW_SUPABASE_PROJECT_REF 与 CLAW_SUPABASE_TOKEN",
                "status": 400}
    try:
        result = supabase_client.push_user_data(username, user_db)
    except supabase_client.SupabaseError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status or 502}
    return {"ok": True, **result}


# ---------- VPS 端点业务体（controller 仍先过 _require_remote_guard）----------

def remote_account_get(username: str) -> dict:
    """GET /api/remote/account：读云端账号行（account 可能为 None）。"""
    username = str(username or "").strip()
    if not username:
        return {"ok": False, "error": "缺少 username", "status": 400}
    if not supabase_client.configured():
        return {"ok": False, "error": "本部署未配置 Supabase，无法作为远程账号权威",
                "status": 400}
    try:
        cloud_log(f"VPS←C 端 读账号 user={username}")
        account = supabase_client.fetch_account(username)
        cloud_log(f"VPS←C 端 读账号完成 user={username} 命中={'是' if account else '否'}")
    except supabase_client.SupabaseError as exc:
        cloud_log(f"VPS←C 端 读账号失败 user={username}: {exc}")
        return {"ok": False, "error": str(exc), "status": exc.status or 502}
    return {"ok": True, "account": account}


def remote_account_post(payload: dict) -> dict:
    """POST /api/remote/account：upsert 账号凭据进 claw_users。"""
    username = str((payload or {}).get("username") or "").strip()
    if not username:
        return {"ok": False, "error": "缺少 username", "status": 400}
    if not supabase_client.configured():
        return {"ok": False, "error": "本部署未配置 Supabase，无法作为远程账号权威",
                "status": 400}
    try:
        cloud_log(f"VPS←C 端 upsert 账号 user={username} "
                  f"api_key=({'有' if str(payload.get('api_key') or '') else '无'})")
        supabase_client.push_user_credentials(
            username,
            api_key=str(payload.get("api_key") or ""),
            site_origin=str(payload.get("site_origin") or ""),
            password_hash=str(payload.get("password_hash") or ""),
            salt=str(payload.get("salt") or ""),
            created_at=str(payload.get("created_at") or ""),
        )
        cloud_log(f"VPS←C 端 upsert 账号完成 user={username}")
    except supabase_client.SupabaseError as exc:
        cloud_log(f"VPS←C 端 upsert 账号失败 user={username}: {exc}")
        return {"ok": False, "error": str(exc), "status": exc.status or 502}
    return {"ok": True, "username": username}


def remote_sync(payload: dict) -> dict:
    """POST /api/remote/sync：direction=push 推云端；默认 pull 回全量行。"""
    username = str((payload or {}).get("username") or "").strip()
    direction = str((payload or {}).get("direction") or "pull").lower()
    if not username:
        return {"ok": False, "error": "缺少 username", "status": 400}
    if not supabase_client.configured():
        return {"ok": False, "error": "本部署未配置 Supabase，无法作为远程同步权威",
                "status": 400}
    try:
        if direction == "push":
            galleries = payload.get("galleries") or []
            runs = payload.get("runs") or []
            cloud_log(f"VPS←C 端 推送 user={username} "
                      f"作品={len(galleries)} 批次={len(runs)}")
            pushed = supabase_client.push_user(username, galleries, runs)
            cloud_log(f"VPS←C 端 推送完成 user={username} 远端={pushed}")
            return {"ok": True, "username": username, "pushed": pushed}
        cloud_log(f"VPS←C 端 拉取 user={username}")
        data = supabase_client.fetch_user(username)
        cloud_log(f"VPS←C 端 拉取完成 user={username} "
                  f"作品={len(data['galleries'])} 批次={len(data['runs'])}")
        return {"ok": True, "username": username,
                "galleries": data["galleries"], "runs": data["runs"],
                "counts": {"galleries": len(data["galleries"]),
                            "runs": len(data["runs"])}}
    except supabase_client.SupabaseError as exc:
        cloud_log(f"VPS←C 端 同步失败 user={username} "
                  f"direction={direction}: {exc}")
        return {"ok": False, "error": str(exc), "status": exc.status or 502}
