"""SQLite 存取层（兼容壳）：实际定义已拆到 ``app/dao/`` 包。

按域拆成 schema / clustering / galleries / runs / users / settings / jobs /
cloud_merge 八个子模块。本模块 re-export 全部符号（含下划线私有的，
``import *`` 不会带下划线名，故逐一显式列出），让所有 ``from app import db;
db.X`` 调用零改动。行为与拆分前完全一致。

去重分三层：
1. gallery_id 主键：同一 ID 永远只保留一行（幂等 upsert）；
2. original_key：详情 API 的原始标题（日文）归一化后的精确键 —— 同一作品的不同语言版本共用；
3. 标题指纹模糊合并：对还没有详情数据的条目做保守合并（卷号签名 + 前缀/相似度 + 括号守卫）。
"""
from __future__ import annotations

# schema：基础层（建表/连接/迁移/通用工具/写锁/列常量）
# 注意：旧 db.py 在模块级 import 了 config / titles，外部（tests/test_auth.py）
# 曾用 db.config 取到 config 模块。这里保留同名引用，行为与拆分前一致。
from . import config  # noqa: F401
from .dao.schema import (  # noqa: F401
    SCHEMA, MIGRATIONS, utc_now, connect, init_db,
    _WRITE_LOCK, _chunks, _optional_int,
    _GALLERY_COLUMNS, _GALLERY_INT_COLUMNS, _GALLERY_MARK_COLUMNS,
    _GALLERY_CONTENT_COLUMNS, _RUN_COLUMNS, _RUN_INT_COLUMNS,
    columns_for, int_columns_for,
)

# clustering：读行 + 去重聚类 + 选版/序列化/排序
from .dao.clustering import (  # noqa: F401
    _row_list, _fetch_rows, _cluster_rows, _language_order, _variant_rank,
    _union, _first_value, _epoch_from_iso, _serialize_group,
    _original_sort_key, _priority_sort_key, _sort_items,
)

# galleries：入库/详情补全/查询/标记/封面/目标
from .dao.galleries import (  # noqa: F401
    UPSERT_SQL, APPLY_DETAIL_SQL,
    _item_to_row, _as_list, _json_list, _params_from_row,
    _CONTENT_FIELDS, _CONTENT_JSON_FIELDS, _same_json,
    content_differs, upsert_items, ids_needing_cover, cover_urls, set_cover,
    ids_needing_detail, apply_details, query_works,
    set_want_download, list_batch_targets, list_want_download_targets,
    set_downloaded, set_hidden, set_site_favorite, get_title,
)

# runs：抓取批次与库统计
from .dao.runs import (  # noqa: F401
    create_run, finish_run, last_run, latest_run_for_url, delete_run,
    merge_runs_for_url, reset_all, list_batches, stats,
)

# users：账号/会话/每用户独立库/跨库标记
from .dao.users import (  # noqa: F401
    user_db_path, hash_password, register_user, verify_user, create_session,
    get_user_by_session, get_user, update_user_settings, upsert_user_credentials,
    delete_session, list_usernames, iter_user_db_paths, mark_downloaded_all,
)

# settings：实例级设置与站点地址推导
from .dao.settings import (  # noqa: F401
    get_setting, set_setting, get_site_origin,
)

# jobs：下载任务队列
from .dao.jobs import (  # noqa: F401
    create_job, update_job, get_job, list_jobs, claim_next_job,
    cancel_job, cancel_queued_jobs, reset_stale_jobs, clear_failed_jobs,
)

# scrape_jobs：抓取队列持久化（重启恢复）
from .dao.scrape_jobs import (  # noqa: F401
    insert_scrape_job, list_pending_scrape_jobs, update_scrape_job,
    cancel_queued_scrape_jobs, reset_running_scrape_jobs,
    cleanup_finished_scrape_jobs,
)

# cloud_merge：云端行导出/合并写本地
from .dao.cloud_merge import (  # noqa: F401
    all_gallery_rows, all_run_rows, row_updated_at,
    upsert_gallery_rows_from_cloud, upsert_run_rows_from_cloud,
)
