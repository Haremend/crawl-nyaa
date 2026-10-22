"""端口独占回归测试。

背景：``http.server.HTTPServer`` 默认 ``allow_reuse_address = True``（设 SO_REUSEADDR）。
在 Windows 上 SO_REUSEADDR 语义宽松，允许**多个进程同时绑同一端口**，谁抢到连接
谁应答 —— 导致旧的 ``python run.py`` 实例叠在新实例下面、持续应答旧代码
（排查「demo 登录不上」时踩到：4 个旧进程同绑 8787，新代码一次没跑起来）。

修复：Windows 改用 ``SO_EXCLUSIVEADDRUSE``（禁止别的进程再来绑，本进程仍能绑过
TIME_WAIT 快速重启）；Linux 保持默认（其 SO_REUSEADDR 本就不允许多进程同绑）。

本测试跨平台验证：同一端口再起一个 ``ApiServer`` 必须抛 ``OSError``。
"""
import shutil
import socket
import threading
import unittest
import uuid
from pathlib import Path

from app.server import ApiServer, Handler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class PortExclusiveBindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("port_bind")
        self.db_path = self.tmp / "main.sqlite3"
        self.servers = []   # (server, thread) 元组

    def tearDown(self):
        for srv, thr in self.servers:
            try:
                srv.shutdown()  # serve_forever 已起线程，shutdown 不会卡
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
            thr.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn(self):
        """起一个绑空闲端口的 ApiServer + serve_forever 线程。"""
        srv = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        thr = threading.Thread(target=srv.serve_forever, daemon=True)
        thr.start()
        self.servers.append((srv, thr))
        return srv

    def test_second_bind_to_same_port_fails(self):
        """同一端口已有一个活监听器时，第二个 ApiServer 必须绑失败。"""
        srv1 = self._spawn()
        port = srv1.server_address[1]  # OS 分配的空闲端口
        # __init__ 已调 listen() 占住端口；同端口再起一个必须 OSError
        with self.assertRaises(OSError):
            ApiServer(("127.0.0.1", port), Handler, db_path=self.db_path)

    def test_second_bind_after_shutdown_succeeds(self):
        """关闭第一个后，同端口应能立即重绑（验证修复没破坏快速重启）。"""
        srv1 = self._spawn()
        port = srv1.server_address[1]
        srv1.shutdown()
        srv1.server_close()
        # 立即重绑应成功（Windows 靠 SO_EXCLUSIVEADDRUSE / TIME_WAIT 不挡 listener）
        srv2 = ApiServer(("127.0.0.1", port), Handler, db_path=self.db_path)
        thr2 = threading.Thread(target=srv2.serve_forever, daemon=True)
        thr2.start()
        self.servers.append((srv2, thr2))
        self.assertEqual(srv2.server_address[1], port)


if __name__ == "__main__":
    unittest.main()
