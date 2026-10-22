"""controller 层：把 app/server.py Handler 的 HTTP 解析/格式化残壳按功能域拆出。

设计（java-web 三层）：
- controller：只读请求参数（query/body）→ 调 service → 用 Handler 的 _json/_error/
  _send 格式化响应。不含业务逻辑。
- service（app/services/）：业务编排（cloud/remote/local 分支、上游拉取、落盘等）。
- dao（app/dao/）：数据存取（SQLite / Supabase）。

路由入口在 app/controllers/routes.py 的 ROUTES + dispatch；Handler.do_GET/
do_POST 只负责 静态文件分派 + _cloud_login_guard + 查 ROUTES 表调对应 controller。
"""
from app.controllers.routes import known, dispatch

__all__ = ["known", "dispatch"]
