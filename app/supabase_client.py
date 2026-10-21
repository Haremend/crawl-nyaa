"""云端数据库（Supabase）客户端：建表 + 按用户双向同步爬取记录。

设计要点
--------
* 只依赖标准库：通过 Supabase 项目的 Postgres REST 查询端点
  （``https://<ref>.supabase.co/database/query``）执行 SQL，
  用 Service Role Key（``sb_secret_...``）做 Bearer 认证，不引入第三方 SDK。
  该端点接受 JSON 形如 ``{"query": "sql"}`` 的 POST 请求，返回 JSON 行数组。
* 旧版走 Management API（``api.supabase.com``）需要账号级 PAT，已弃用；
  Service Role Key 直连项目数据库即可执行 DDL 与数据读写。
* 云端表名统一加前缀（默认 claw_），表结构按「多租户」设计：
  每张业务表都带 username 列并作为复合主键的一部分，一个用户一套数据。
* 本地模式：数据仍以本地 SQLite 为准，云端是跨部署共享的副本；
  「同步」= 先拉云端合并进本地（较新者胜），再把合并后的本地数据推回云端。
  云模式（CLAW_STORAGE_BACKEND=cloud）：业务数据直接读写云端，
  不落本地 SQLite，此时没有同步操作（数据本就在云端）。
* 所有表都开启 RLS 并撤销 anon / authenticated 的直接表权限；
  读写一律走 Service Role（bypass RLS），不创建面向客户端的开放策略。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterable

from . import config, db


class SupabaseError(RuntimeError):
    """云端数据库调用失败。"""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = int(status or 0)


# ============================================================
# SQL 字面量与标识符
# ============================================================

def _ident(name):
    """把标识符安全地包成 "xxx"（内部双引号翻倍）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _literal(value):
    """把 Python 值转成 Postgres 字面量（字符串统一做转义）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value)
    if "\\" in text:
        # E'' 形式：反斜杠需要翻倍，单引号同样翻倍即可
        return "E'" + text.replace("\\", "\\\\").replace("'", "''") + "'"
    return "'" + text.replace("'", "''") + "'"


def _bool_int(value):
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
    return 1 if value else 0


# ============================================================
# 传输层（可注入，便于离线测试）
# ============================================================

class Transport:
    """通过 Supabase Management API 的 SQL 查询端点执行 SQL。

    端点：POST {api_url}/v1/projects/{project_ref}/database/query，
    认证：Authorization: Bearer <PAT>（账号级 Personal Access Token）。

    注意：Supabase 没有「纯 HTTP + Service Role Key 执行 SQL」的端点——
    Service Role Key（sb_secret_...）走 Postgres TCP 直连（需 psycopg，违反
    零依赖约束）；HTTP 路径上只有 Management API 的 /database/query，
    它只接受账号级 PAT。因此 CLAW_SUPABASE_TOKEN 应填 PAT。
    """

    def __init__(self, api_url, project_ref, token, timeout=30.0):
        self.api_url = (api_url or "https://api.supabase.com").rstrip("/")
        self.project_ref = project_ref
        self.token = token
        self.timeout = float(timeout or 30.0)

    def query(self, sql, *, read_only=False):
        url = f"{self.api_url}/v1/projects/{self.project_ref}/database/query"
        payload = json.dumps({"query": sql}).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": config.USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # pragma: no cover - 读取失败也不该崩
                detail = ""
            raise SupabaseError(
                f"云端返回 HTTP {exc.code}：{detail or exc.reason}", exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise SupabaseError(f"无法连接云端数据库：{exc.reason}", 0) from exc
        except OSError as exc:
            raise SupabaseError(f"无法连接云端数据库：{exc}", 0) from exc
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SupabaseError(f"云端返回内容不是合法 JSON：{raw[:200]}", 0) from exc
        if isinstance(data, dict):
            if "rows" in data:
                return list(data["rows"])
            message = data.get("error") or data.get("message") or raw[:200]
            raise SupabaseError(f"云端执行失败：{message}", 400)
        return list(data) if isinstance(data, list) else []


_transport = None


def set_transport(transport):
    """注入自定义传输层（测试用）；传 None 恢复默认。"""
    global _transport
    _transport = transport


def get_transport():
    global _transport
    if _transport is None:
        _transport = Transport(
            config.SUPABASE_API_URL, config.SUPABASE_PROJECT_REF,
            config.SUPABASE_TOKEN, config.SUPABASE_TIMEOUT,
        )
    return _transport


def configured():
    return config.supabase_configured()


def _require_config():
    if not config.supabase_configured():
        raise SupabaseError(
            "未配置云端数据库：请在 .env 里填写 CLAW_SUPABASE_PROJECT_REF 与 CLAW_SUPABASE_TOKEN",
            400,
        )


def run_sql(sql, *, read_only=False):
    _require_config()
    return get_transport().query(sql, read_only=read_only)


# ============================================================
# 表结构
# ============================================================

def table(name):
    return f"{config.SUPABASE_TABLE_PREFIX}{name}"


def _q(name):
    """带前缀且已加引号的表名（形如 "claw_galleries"）。"""
    return _ident(table(name))


USERS_TABLE = "users"
GALLERIES_TABLE = "galleries"
RUNS_TABLE = "scrape_runs"

_GALLERY_DDL_COLUMNS = (
    ("username", "text not null"),
    ("gallery_id", "bigint not null"),
    ("media_id", "bigint"),
    ("title", "text not null default ''"),
    ("title_pretty", "text not null default ''"),
    ("title_original", "text not null default ''"),
    ("title_key", "text not null default ''"),
    ("original_key", "text not null default ''"),
    ("url", "text not null default ''"),
    ("thumbnail", "text not null default ''"),
    ("languages", "text not null default '[]'"),
    ("primary_language", "text not null default 'unknown'"),
    ("tags", "text not null default '[]'"),
    ("artists", "text not null default '[]'"),
    ("groups", "text not null default '[]'"),
    ("parodies", "text not null default '[]'"),
    ("characters", "text not null default '[]'"),
    ("categories", "text not null default '[]'"),
    ("cover_file", "text not null default ''"),
    ("cover_fetched_at", "text not null default ''"),
    ("scanlator", "text not null default ''"),
    ("uncensored", "boolean not null default false"),
    ("want_download", "boolean not null default false"),
    ("downloaded", "boolean not null default false"),
    ("downloaded_at", "text not null default ''"),
    ("hidden", "boolean not null default false"),
    ("site_favorite", "boolean not null default false"),
    ("from_scrape", "boolean not null default true"),
    ("batch_id", "bigint not null default 0"),
    ("num_pages", "bigint"),
    ("num_favorites", "bigint"),
    ("upload_date", "text not null default ''"),
    ("source_url", "text not null default ''"),
    ("detail_fetched_at", "text not null default ''"),
    ("first_seen", "text not null default ''"),
    ("last_seen", "text not null default ''"),
)

_RUNS_DDL_COLUMNS = (
    ("username", "text not null"),
    ("url", "text not null"),
    ("run_id", "bigint"),
    ("status", "text not null default ''"),
    ("started_at", "text not null default ''"),
    ("finished_at", "text not null default ''"),
    ("pages_total", "integer not null default 0"),
    ("pages_done", "integer not null default 0"),
    ("items_seen", "integer not null default 0"),
    ("items_new", "integer not null default 0"),
    ("items_dup", "integer not null default 0"),
    ("error", "text"),
)


def _columns_sql(columns):
    lines = [f"  {_ident(col)} {ctype}" for col, ctype in columns]
    lines.append('  "synced_at" timestamptz not null default now()')
    return ",\n".join(lines)


def ddl_statements():
    """建表语句（幂等）。列名与 SQLite 保持一致，便于整行同步。

    末尾的 REVOKE 撤销 anon / authenticated 的直接表权限：
    配合 RLS（无策略 = 默认拒绝），只有走 Service Role（bypass RLS）
    的服务端凭据才能读写；客户端永远拿不到表数据。
    """
    galleries_body = _columns_sql(_GALLERY_DDL_COLUMNS) + ',\n  primary key ("username", "gallery_id")'
    runs_body = _columns_sql(_RUNS_DDL_COLUMNS) + ',\n  primary key ("username", "url")'
    stmts = [
        f'''create table if not exists public.{_q(USERS_TABLE)} (
  "username" text primary key,
  "password_hash" text not null default '',
  "salt" text not null default '',
  "api_key" text not null default '',
  "site_origin" text not null default '',
  "created_at" text not null default '',
  "synced_at" timestamptz not null default now()
)''',
        f"alter table public.{_q(USERS_TABLE)} enable row level security",
        f"create table if not exists public.{_q(GALLERIES_TABLE)} (\n{galleries_body}\n)",
        'create index if not exists {idx} on public.{tbl} ("username", "last_seen" desc)'.format(
            idx=_ident(table(GALLERIES_TABLE) + "_user_seen_idx"), tbl=_q(GALLERIES_TABLE)),
        f"alter table public.{_q(GALLERIES_TABLE)} enable row level security",
        f"create table if not exists public.{_q(RUNS_TABLE)} (\n{runs_body}\n)",
        'create index if not exists {idx} on public.{tbl} ("username")'.format(
            idx=_ident(table(RUNS_TABLE) + "_user_idx"), tbl=_q(RUNS_TABLE)),
        f"alter table public.{_q(RUNS_TABLE)} enable row level security",
    ]
    for name in (USERS_TABLE, GALLERIES_TABLE, RUNS_TABLE):
        stmts.append(
            f"revoke all on table public.{_q(name)} from anon, authenticated"
        )
    return stmts


def ensure_tables():
    """建表（幂等），返回创建/确认的表名列表。"""
    for statement in ddl_statements():
        run_sql(statement)
    return {"ok": True, "tables": [
        f"public.{table(USERS_TABLE)}",
        f"public.{table(GALLERIES_TABLE)}",
        f"public.{table(RUNS_TABLE)}",
    ]}


def tables_exist():
    """检查三张云端表是否都已存在。"""
    names = [table(USERS_TABLE), table(GALLERIES_TABLE), table(RUNS_TABLE)]
    rows = run_sql(
        "select table_name from information_schema.tables "
        "where table_schema = 'public' and table_name in ("
        + ", ".join(_literal(name) for name in names) + ")",
        read_only=True,
    )
    found = {str(row.get("table_name") or "") for row in rows}
    return all(name in found for name in names)


# ============================================================
# 行转换
# ============================================================

_GALLERY_BOOL_COLUMNS = (
    "uncensored", "want_download", "downloaded", "hidden", "site_favorite", "from_scrape",
)


def _gallery_value(row, column):
    value = row.get(column)
    if column in _GALLERY_BOOL_COLUMNS:
        return "TRUE" if _bool_int(value) else "FALSE"
    if column in ("media_id", "num_pages", "num_favorites"):
        return _literal(db._optional_int(value))
    if column in ("batch_id", "gallery_id"):
        return _literal(int(value or 0))
    return _literal(value if value is not None else "")


def _run_value(row, column):
    if column in ("pages_total", "pages_done", "items_seen", "items_new", "items_dup"):
        return _literal(int(row.get(column) or 0))
    if column == "run_id":
        return _literal(db._optional_int(row.get(column)))
    if column == "error":
        return _literal(row.get(column))
    return _literal(row.get(column) if row.get(column) is not None else "")


def _row_values(row, table):
    is_gallery = table == "galleries"
    return [
        _gallery_value(row, col) if is_gallery else _run_value(row, col)
        for col in db.columns_for(table)
    ]


def _upsert_sql(table):
    """构造推送用的 upsert SQL。

    galleries：标记字段用 ``GREATEST(本地, excluded)`` 合并（1 覆盖 0），
    内容字段仅当 excluded.last_seen >= 本地时覆盖，时间戳取极值；
    不再用 WHERE 守卫跳过整行 —— 标记始终合并。
    scrape_runs：按时间较新者胜（原有守卫不变）。
    """
    columns = db.columns_for(table)
    cols = ", ".join(_ident(col) for col in columns)
    conflict = '("username", "gallery_id")' if table == "galleries" else '("username", "url")'
    if table == "galleries":
        mark_set = ", ".join(
            f'{_ident(c)} = GREATEST(public.{_q(table)}.{_ident(c)}, excluded.{_ident(c)})'
            for c in db._GALLERY_MARK_COLUMNS)
        content_set = ", ".join(
            f'{_ident(c)} = CASE WHEN excluded."last_seen" >= public.{_q(table)}."last_seen" '
            f"THEN excluded.{_ident(c)} ELSE public.{_q(table)}.{_ident(c)} END"
            for c in db._GALLERY_CONTENT_COLUMNS)
        time_set = (
            f'"first_seen" = LEAST(public.{_q(table)}."first_seen", excluded."first_seen"), '
            f'"last_seen" = GREATEST(public.{_q(table)}."last_seen", excluded."last_seen")')
        updates = ", ".join(p for p in (mark_set, content_set, time_set) if p)
        return (
            f"insert into public.{_q(table)} (\"username\", {cols}) values {{values}} "
            f"on conflict {conflict} do update set {updates}, \"synced_at\" = now()"
        )
    updates = ", ".join(
        f"{_ident(col)} = excluded.{_ident(col)}" for col in columns if col != "username"
    )
    guard = (
        "coalesce(nullif(excluded.\"finished_at\", ''), nullif(excluded.\"started_at\", ''), '') >= "
        f"coalesce(nullif(public.{_q(table)}.\"finished_at\", ''), "
        f"nullif(public.{_q(table)}.\"started_at\", ''), '')"
    )
    return (
        f"insert into public.{_q(table)} (\"username\", {cols}) values {{values}} "
        f"on conflict {conflict} do update set {updates}, \"synced_at\" = now() "
        f"where {guard}"
    )


# ============================================================
# 推送 / 拉取
# ============================================================

def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _values_group(row, table, username_literal):
    return "(" + username_literal + ", " + ", ".join(_row_values(row, table)) + ")"


def push_user(username, galleries, runs):
    """把本地行推到云端（按主键 upsert，较新者胜）。"""
    _require_config()
    name = _literal(str(username))
    gallery_rows = [dict(row) for row in galleries]
    run_rows = [dict(row) for row in runs]
    batch = 200
    pushed = {"galleries": 0, "runs": 0}
    for table, rows, key in (("galleries", gallery_rows, "galleries"),
                             ("scrape_runs", run_rows, "runs")):
        if not rows:
            continue
        sql = _upsert_sql(table)
        for chunk in _chunks(rows, batch):
            body = ", ".join(_values_group(row, table, name) for row in chunk)
            run_sql(sql.format(values=body))
            pushed[key] += len(chunk)
    return pushed


def _to_bool_int(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
    return 1 if value else 0


def _normalize_gallery_row(row):
    out = dict(row)
    out.pop("username", None)
    out.pop("synced_at", None)
    for col in _GALLERY_BOOL_COLUMNS:
        if col in out:
            out[col] = _to_bool_int(out[col])
    for col in ("media_id", "num_pages", "num_favorites", "batch_id", "gallery_id"):
        if col in out and out[col] is not None:
            try:
                out[col] = int(out[col])
            except (TypeError, ValueError):
                out[col] = None
    return out


def _normalize_run_row(row):
    out = dict(row)
    out.pop("username", None)
    out.pop("synced_at", None)
    for col in ("pages_total", "pages_done", "items_seen", "items_new", "items_dup", "run_id"):
        if col in out and out[col] is not None:
            try:
                out[col] = int(out[col])
            except (TypeError, ValueError):
                out[col] = None
    return out


def fetch_user(username):
    """拉取某个用户在云端的全部作品与批次行。"""
    _require_config()
    name = _literal(str(username))
    gallery_cols = ", ".join(_ident(col) for col in db.columns_for("galleries"))
    run_cols = ", ".join(_ident(col) for col in db.columns_for("scrape_runs"))
    galleries = run_sql(
        f"select {gallery_cols} from public.{_q(GALLERIES_TABLE)} "
        f"where \"username\" = {name}",
        read_only=True,
    )
    runs = run_sql(
        f"select {run_cols} from public.{_q(RUNS_TABLE)} where \"username\" = {name}",
        read_only=True,
    )
    return {
        "galleries": [_normalize_gallery_row(row) for row in galleries],
        "runs": [_normalize_run_row(row) for row in runs],
    }


def pull_user(username, local_path=None):
    """拉取：从云端拉该用户全部作品/批次，insert 或 update（1 覆盖 0）进本地。"""
    _require_config()
    ensure_tables()
    cloud = fetch_user(username)
    pulled = {
        "galleries": db.upsert_gallery_rows_from_cloud(cloud["galleries"], local_path),
        "runs": db.upsert_run_rows_from_cloud(cloud["runs"], local_path),
    }
    return {
        "username": username,
        "cloud": {"galleries": len(cloud["galleries"]), "runs": len(cloud["runs"])},
        "pulled": pulled,
        "local": {
            "galleries": len(db.all_gallery_rows(local_path)),
            "runs": len(db.all_run_rows(local_path)),
        },
    }


def push_user_data(username, local_path=None):
    """推送：把本地全部作品/批次 insert 或 update（1 覆盖 0）覆盖到远端。"""
    _require_config()
    ensure_tables()
    limit = config.SUPABASE_SYNC_MAX_ROWS
    local_galleries = db.all_gallery_rows(local_path)[:limit]
    local_runs = db.all_run_rows(local_path)[:limit]
    pushed = push_user(username, local_galleries, local_runs)
    return {
        "username": username,
        "pushed": pushed,
        "local": {"galleries": len(local_galleries), "runs": len(local_runs)},
    }


def sync_user(username, local_path=None, *, pull=True, push=True):
    """双向同步（拉取 + 推送一次性完成）。保留兼容旧 /api/sync。"""
    _require_config()
    ensure_tables()
    result = {"username": username,
              "pulled": {"galleries": 0, "runs": 0},
              "pushed": {"galleries": 0, "runs": 0},
              "cloud": {"galleries": 0, "runs": 0},
              "local": {"galleries": 0, "runs": 0}}
    if pull:
        r = pull_user(username, local_path)
        result["pulled"] = r["pulled"]
        result["cloud"] = r["cloud"]
        result["local"] = r["local"]
    if push:
        r = push_user_data(username, local_path)
        result["pushed"] = r["pushed"]
        if not pull:
            result["local"] = r["local"]
    return result


def push_user_credentials(username, *, api_key, site_origin,
                          password_hash="", salt="", created_at=""):
    """把账号的 API Key / 站点域名同步到云端账号表（登录后跨设备可用）。"""
    _require_config()
    cols = ["username", "password_hash", "salt", "api_key", "site_origin", "created_at"]
    values = [_literal(username), _literal(password_hash), _literal(salt),
              _literal(api_key), _literal(site_origin), _literal(created_at)]
    updates = ", ".join(
        f"{_ident(col)} = excluded.{_ident(col)}"
        for col in ("password_hash", "salt", "api_key", "site_origin", "created_at")
    )
    sql = (
        f'insert into public.{_q(USERS_TABLE)} ({", ".join(_ident(c) for c in cols)}) '
        f'values ({", ".join(values)}) on conflict ("username") do update set '
        f'{updates}, "synced_at" = now()'
    )
    run_sql(sql)
    return {"ok": True, "username": username}


_ACCOUNT_COLS = ["username", "password_hash", "salt", "api_key", "site_origin", "created_at"]


def _account_columns_sql():
    return ", ".join(_ident(c) for c in _ACCOUNT_COLS)


def _account_row(row: dict) -> dict:
    """把云端账号行规整成与本地 users 表一致的字段（去掉云端特有列）。"""
    out = {c: row.get(c) for c in _ACCOUNT_COLS}
    out.pop("created_at", None)
    for c in _ACCOUNT_COLS:
        if out.get(c) is None:
            out[c] = ""
    return out


def fetch_account(username) -> dict | None:
    """拉取单个账号记录（不存在返回 None）。"""
    _require_config()
    ensure_tables()
    rows = run_sql(
        f'select {_account_columns_sql()} from public.{_q(USERS_TABLE)} '
        f'where "username" = {_literal(str(username))}',
        read_only=True,
    )
    if not rows:
        return None
    return _account_row(rows[0])


def fetch_accounts_all() -> list[dict]:
    """拉取全部账号（供本地模式启动时把云端账号加载进本地 users 表）。"""
    _require_config()
    ensure_tables()
    rows = run_sql(
        f'select {_account_columns_sql()} from public.{_q(USERS_TABLE)} order by "username"',
        read_only=True,
    )
    return [_account_row(r) for r in rows]


# ============================================================
# 云模式：业务数据直接读写云端（CLAW_STORAGE_BACKEND=cloud）
# 本地模式不走这些；这里只是把 SQL 语义封装出来供 server 分叉调用。
# ============================================================

def cloud_mark_gallery_flags(username, gallery_ids, *, flag_col: str, value: bool) -> int:
    """批量设置某用户的作品标记（want_download / downloaded / hidden / site_favorite）。

    用 OR 合并（置 1）或 AND 语义（置 0）避免旧副本意外清空：
    - value=True：``flag = true``（直接覆盖，因为「标记」本身是用户主动行为）
    - value=False：``flag = false``（取消标记）
    返回受影响的行数。
    """
    _require_config()
    ensure_tables()
    ids = [int(g) for g in gallery_ids if int(g or 0) > 0]
    if not ids:
        return 0
    marks = ", ".join(_literal(i) for i in ids)
    setter = "true" if value else "false"
    sql = (
        f'update public.{_q(GALLERIES_TABLE)} '
        f'set "{flag_col}" = {setter} where "username" = {_literal(str(username))} '
        f'and "gallery_id" in ({marks})'
    )
    run_sql(sql)
    return len(ids)


def cloud_fetch_gallery_rows(username) -> list[dict]:
    """拉取某用户全部作品行（云模式浏览/筛选/排序的数据源）。"""
    data = fetch_user(username)
    return data["galleries"]


def cloud_fetch_run_rows(username) -> list[dict]:
    """拉取某用户全部批次行（云模式批次列表的数据源）。"""
    data = fetch_user(username)
    return data["runs"]