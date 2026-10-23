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
    base = {
        "ok": True,
        "unlogin_user": config.UNLOGIN_USER,
    }
    if config.storage_is_cloud():
        base.update({"configured": True, "available": True,
                     "cloud_mode": True,
                     "message": "云模式：数据直连云端，无需同步"})
        return base
    if config.remote_configured():
        base.update({"configured": True, "available": True,
                     "cloud_mode": False, "via_remote": True,
                     "remote_base_masked": remote_base_masked(),
                     "message": "已通过远程 VPS 同步（本机无需 Supabase 凭据）。"})
        return base
    configured = supabase_client.configured()
    available = False
    if configured:
        try:
            available = supabase_client.tables_exist()
        except supabase_client.SupabaseError:
            available = False
    base.update({"configured": configured, "available": available,
                 "cloud_mode": False, "via_remote": False,
                 "remote_base_masked": remote_base_masked()})
    return base


def _cross_merge(target_user: str, target_db: str | Path,
                 source_db: str | Path, extra_source: str = "") -> dict:
    """跨账号合并（需求基线 §12.3）：以 target 的本地库为合并落点。

    步骤：①拉 target 的云端数据 → 合并进 target 本地库；
         ②若 source_db ≠ target_db，把 source 本地库的行按同一合并规则
           （cloud_merge：标记列 1 覆盖 0、last_seen 新者胜）并入 target 本地；
         ③（可选）extra_source 是第三方账号名时，拉其云端行也并入 target 本地；
         ④把 target 本地（已合并）推回 target 的云端账号。
    合并全程复用 db.upsert_gallery_rows_from_cloud / upsert_run_rows_from_cloud，
    不新增规则。source_db == target_db 时退化为常规单账号同步（拉→推自己）。
    """
    limit = config.SUPABASE_SYNC_MAX_ROWS

    def _merge_rows(g_rows, r_rows, into_db):
        g = db.upsert_gallery_rows_from_cloud(g_rows or [], into_db)
        r = db.upsert_run_rows_from_cloud(r_rows or [], into_db)
        return g, r

    if config.remote_configured():
        client = remote_client.get_client()
        try:
            res = client.pull(target_user)
            pulled_g, pulled_r = _merge_rows(res.get("galleries"), res.get("runs"), target_db)
        except remote_client.RemoteError as exc:
            return {"ok": False, "error": f"拉取 {target_user} 云端数据失败：{exc}",
                    "status": 502}
        merged_g = merged_r = 0
        if Path(source_db) != Path(target_db):
            merged_g, merged_r = _merge_rows(
                db.all_gallery_rows(source_db)[:limit],
                db.all_run_rows(source_db)[:limit], target_db)
        if extra_source and extra_source != target_user:
            try:
                ex = client.pull(extra_source)
                _merge_rows(ex.get("galleries"), ex.get("runs"), target_db)
            except remote_client.RemoteError:
                pass  # 第三方源拉取失败不阻断主流程
        try:
            res2 = client.push(
                target_user,
                db.all_gallery_rows(target_db)[:limit],
                db.all_run_rows(target_db)[:limit])
        except remote_client.RemoteError as exc:
            return {"ok": False, "error": f"推送 {target_user} 云端数据失败：{exc}",
                    "status": 502}
        return {"ok": True, "username": target_user, "target_user": target_user,
                "source_user": extra_source or "",
                "pulled": {"galleries": pulled_g, "runs": pulled_r},
                "merged": {"galleries": merged_g, "runs": merged_r},
                "pushed": res2.get("pushed", {}),
                "cloud": res.get("counts", {}),
                "local": {"galleries": len(db.all_gallery_rows(target_db)),
                           "runs": len(db.all_run_rows(target_db))},
                "via_remote": True}
    if not supabase_client.configured():
        return {"ok": False,
                "error": "未配置云端数据库：请在 .env 里填写 CLAW_SUPABASE_PROJECT_REF 与 CLAW_SUPABASE_TOKEN",
                "status": 400}
    try:
        data = supabase_client.fetch_user(target_user)
        pulled_g, pulled_r = _merge_rows(data["galleries"], data["runs"], target_db)
        merged_g = merged_r = 0
        if Path(source_db) != Path(target_db):
            merged_g, merged_r = _merge_rows(
                db.all_gallery_rows(source_db)[:limit],
                db.all_run_rows(source_db)[:limit], target_db)
        if extra_source and extra_source != target_user:
            ex = supabase_client.fetch_user(extra_source)
            _merge_rows(ex["galleries"], ex["runs"], target_db)
        supabase_client.push_user_data(target_user, target_db)
        return {"ok": True, "username": target_user, "target_user": target_user,
                "source_user": extra_source or "",
                "pulled": {"galleries": pulled_g, "runs": pulled_r},
                "merged": {"galleries": merged_g, "runs": merged_r},
                "local": {"galleries": len(db.all_gallery_rows(target_db)),
                           "runs": len(db.all_run_rows(target_db))}}
    except supabase_client.SupabaseError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status or 502}


def cross_sync(user: dict, target_user: str = "", source_user: str = "") -> dict:
    """跨账号同步（需求基线 §12.3/§12.5）：

    - 未登录：源本地库 = unlogin_user；target_user 缺省 = unlogin_user
      （常规同步），传已登录账号名则把 unlogin 的数据合并进该账号本地库 + 云端。
    - 已登录：源本地库 = 登录账号；target_user 缺省 = 登录账号（常规同步），
      可传 unlogin_user 做反向合并（登录数据并回匿名库）。
    - source_user（请求体可选）：第三方账号名，其云端数据也一并并入 target 本地。
    """
    if config.storage_is_cloud():
        return {"ok": True, "cloud_mode": True,
                "message": "当前为云模式：数据直连云端，无需同步"}
    source_local_user = (user or {}).get("username", "") or config.UNLOGIN_USER
    target_user = str(target_user or "").strip() or source_local_user
    source_db = db.user_db_path(source_local_user)
    target_db = db.user_db_path(target_user)
    db.init_db(source_db)
    db.init_db(target_db)
    return _cross_merge(target_user, target_db, source_db,
                        extra_source=str(source_user or "").strip())


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
