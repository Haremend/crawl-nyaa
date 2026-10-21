# -*- coding: utf-8 -*-
"""测试包入口：锁定本地存储模式 + 未配置 Supabase（保证测试离线、确定）。

discover 时由 Python 包导入机制最先执行（先于各测试模块的 app 导入），
因此这里既清环境变量、又强制纠正已载入的 config 全局。
"""
import os

# 1) 环境变量：本地模式 + 无 Supabase 凭据（防止 .env 的真实 PAT / cloud 默认值泄漏进测试）
os.environ["CLAW_STORAGE_BACKEND"] = "local"
os.environ.pop("CLAW_SUPABASE_PROJECT_REF", None)
os.environ.pop("CLAW_SUPABASE_TOKEN", None)
os.environ.pop("CLAW_SUPABASE_API_URL", None)
os.environ.pop("CLAW_SUPABASE_DB_URL", None)

# 2) 若 app.config 已被更早导入（load_env 载入了 .env），再纠正一次
#    discover 场景下需要先把项目根加入 sys.path 才能 import 到 app
import sys
from pathlib import Path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from app import config as _config
    _config.STORAGE_BACKEND = "local"
    _config.SUPABASE_PROJECT_REF = ""
    _config.SUPABASE_TOKEN = ""
    _config.SUPABASE_API_URL = "https://api.supabase.com"
except Exception:
    pass
