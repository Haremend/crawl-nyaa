"""测试基类：强制本地存储模式 + 未配置 Supabase（所有测试共用）。

用法：每个测试模块顶部 import 本文件即可（``from tests._isolated import _force_local``），
确保 ApiServer / ScrapeService 在本地模式、无云端凭据下运行，不触真实网络。
"""
from app import config as _config


def _force_local() -> None:
    """把全局 config 锁回本地模式 + 未配置 Supabase（测试确定性）。"""
    _config.STORAGE_BACKEND = "local"
    _config.SUPABASE_PROJECT_REF = ""
    _config.SUPABASE_TOKEN = ""
    _config.REMOTE_BASE = ""
    _config.REMOTE_AUTH_TOKEN = ""
    try:
        from app import supabase_client as _sc
        _sc.set_transport(None)
    except Exception:
        pass
    try:
        from app import remote_client as _rc
        _rc.reset_client()
    except Exception:
        pass
