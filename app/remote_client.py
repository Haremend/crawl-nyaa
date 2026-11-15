"""远程 claw-nhentai VPS 的 HTTP 客户端（纯标准库，不走画廊代理）。

C 端（本地模式）设了 ``CLAW_REMOTE_BASE`` 之后，登录/注册/拉取/推送全部改走
这台 VPS 的 REST API，由 VPS 作为唯一的云端权威（它持有 Supabase PAT）。
本地不再需要 ``CLAW_SUPABASE_*`` 凭据。

关键约束：
* 目标是「自己的 VPS」，不是上游内容站 —— 因此**不**走 ``Fetcher()`` /
  ``config.effective_proxy()``（那个是抓站点的画廊代理），用裸 ``urllib``，
  可选按 ``config.INSECURE_TLS`` 关 TLS 校验。
* 访问控制：若配置了 ``CLAW_REMOTE_AUTH_TOKEN``，每个请求带
  ``Authorization: Bearer <token>``；VPS 侧校验不匹配则 403。
* 可注入（测试用 fake）：``set_client`` / ``reset_client``，仿
  ``supabase_client.set_transport``。
"""
from __future__ import annotations

import datetime
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import config


def _log(*parts) -> None:
    """打 stderr：记脱敏字段（base / method / path / HTTP 状态 / 错误摘要），
    **不打** 请求体——里面带账号 API Key（AGENTS.md §1.2：Key 不得写日志）。"""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] [cloud] " + " ".join(str(p) for p in parts), file=sys.stderr)


class RemoteError(RuntimeError):
    """远程 VPS 调用失败。"""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = int(status or 0)


# ---------- 推送分片：把全量行按请求体大小切成多片（VPS 有 1MB 上限）----------

def _payload_size(username: str, galleries: list, runs: list) -> int:
    """按 ``_request`` 的序列化方式算请求体字节数（json.dumps 默认 ensure_ascii）。"""
    return len(json.dumps({"username": username, "direction": "push",
                           "galleries": galleries, "runs": runs}).encode("utf-8"))


def _split_rows(rows: list, budget: int, envelope: int) -> list[list]:
    """按每行的序列化大小贪心装箱，保证「信封 + 一片」不超过 budget 字节。

    单行本身就超预算时仍单独成片（必然被上游拒收，由调用方报错暴露）。
    """
    chunks: list[list] = []
    current: list = []
    size = 0
    for row in rows:
        # "+2" 是数组元素分隔符 ", "，按每行都算一次属于略保守（不会低估）
        row_size = len(json.dumps(row).encode("utf-8")) + 2
        if current and envelope + size + row_size > budget:
            chunks.append(current)
            current, size = [], 0
        current.append(row)
        size += row_size
    if current:
        chunks.append(current)
    return chunks


class RemoteClient:
    """指向一台 claw-nhentai VPS 的最小 HTTP 客户端。"""

    def __init__(self, base=None, *, timeout=None, auth_token=None,
                 chunk_bytes=None) -> None:
        self.base = (base or config.REMOTE_BASE or "").rstrip("/")
        self.timeout = float(timeout or config.REQUEST_TIMEOUT)
        self.user_agent = config.USER_AGENT
        self.auth_token = (config.REMOTE_AUTH_TOKEN if auth_token is None else auth_token)
        self.chunk_bytes = int(config.PUSH_CHUNK_BYTES if chunk_bytes is None
                               else chunk_bytes)

    # ---------- 底层请求 ----------
    def _context(self):
        if not config.INSECURE_TLS:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _request(self, method: str, path: str, *, payload=None) -> dict:
        if not self.base:
            raise RemoteError("未配置远程 VPS：请在 .env 里填写 CLAW_REMOTE_BASE", 0)
        url = f"{self.base}{path}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self._context()) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # pragma: no cover
                detail = ""
            raise RemoteError(f"远程 VPS 返回 HTTP {exc.code}：{detail or exc.reason}",
                              exc.code) from exc
        except urllib.error.URLError as exc:
            raise RemoteError(f"无法连接远程 VPS {self.base}：{exc.reason}", 0) from exc
        except OSError as exc:
            raise RemoteError(f"无法连接远程 VPS {self.base}：{exc}", 0) from exc
        if not raw.strip():
            return {"ok": True}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RemoteError(f"远程 VPS 返回内容不是合法 JSON：{raw[:200]}", 0) from exc
        if isinstance(data, dict) and ("error" in data or "ok" not in data):
            raise RemoteError(f"远程 VPS 执行失败：{data.get('error', '')}", data.get("http", 400))
        return data if isinstance(data, dict) else {"ok": True, "data": data}

    # ---------- 高层操作（形状对齐 supabase_client，便于本地端 drop-in）----------
    def fetch_account(self, username: str):
        """拉某个用户的云端账号行（形状同 supabase_client._account_row）。"""
        q = urllib.parse.urlencode({"username": username})
        _log(f"C 端→VPS 拉取账号 {self.base} user={username}")
        try:
            data = self._request("GET", f"/api/remote/account?{q}")
        except RemoteError as exc:
            _log(f"拉取账号失败 user={username}: {exc} (HTTP {exc.status})")
            raise
        account = data.get("account")
        _log(f"拉取账号完成 user={username} 命中={'是' if account else '否'}")
        return account

    def upsert_account(self, username: str, *, api_key: str, site_origin: str,
                       password_hash: str, salt: str, created_at: str) -> dict:
        """把账号凭据 upsert 进云端 claw_users（远端是账号权威）。"""
        payload = {
            "username": username, "api_key": api_key, "site_origin": site_origin,
            "password_hash": password_hash, "salt": salt, "created_at": created_at,
        }
        _log(f"C 端→VPS upsert 账号 {self.base} user={username} "
             f"api_key=({'有' if api_key else '无'}) site_origin={site_origin or '-'}")
        try:
            res = self._request("POST", "/api/remote/account", payload=payload)
        except RemoteError as exc:
            _log(f"upsert 账号失败 user={username}: {exc} (HTTP {exc.status})")
            raise
        _log(f"upsert 账号完成 user={username}")
        return res

    def pull(self, username: str) -> dict:
        """从远端拉该用户全部作品/批次行，返回 {galleries, runs, counts}。"""
        _log(f"C 端→VPS 拉取作品/批次 {self.base} user={username}")
        try:
            data = self._request("POST", "/api/remote/sync",
                                 payload={"username": username, "direction": "pull"})
        except RemoteError as exc:
            _log(f"拉取作品/批次失败 user={username}: {exc} (HTTP {exc.status})")
            raise
        counts = data.get("counts") or {}
        _log(f"拉取完成 user={username} 作品={counts.get('galleries', 0)} 批次={counts.get('runs', 0)}")
        return data

    def push(self, username: str, galleries: list, runs: list) -> dict:
        """把本地行推给远端做云端合并：按请求体大小自动分片，逐片 upsert。

        远端对请求体有上限（本仓库 ``server._read_json`` 为 1MB），一次性推
        全量在数据量大时必然 413，因此按 ``chunk_bytes``（默认 768KB，可用
        ``CLAW_PUSH_CHUNK_BYTES`` 调整）把 galleries / runs 分别贪心切块、
        逐片 POST。云端侧按主键 upsert（同 ``push_user`` 语义），分片与单片
        等价；中途某片失败时已推的分片保留，重试即可补齐（幂等）。
        """
        galleries = list(galleries or [])
        runs = list(runs or [])
        envelope = _payload_size(username, [], [])   # username 等固定开销
        g_chunks = _split_rows(galleries, self.chunk_bytes, envelope)
        r_chunks = _split_rows(runs, self.chunk_bytes, envelope)
        if g_chunks and r_chunks and _payload_size(username, g_chunks[0], r_chunks[0]) <= self.chunk_bytes:
            # 小数据量：批次行搭第一片，保持「单片推送」的旧行为
            payloads = [(g_chunks[0], r_chunks[0])]
            payloads += [(g, []) for g in g_chunks[1:]]
            payloads += [([], r) for r in r_chunks[1:]]
        elif g_chunks or r_chunks:
            payloads = [(g, []) for g in g_chunks] + [([], r) for r in r_chunks]
        else:
            payloads = [([], [])]   # 空推送也发一次（保持旧行为，远端回 0/0）
        total = len(payloads)
        pushed = {"galleries": 0, "runs": 0}
        _log(f"C 端→VPS 推送作品/批次 {self.base} user={username} "
             f"推送作品={len(galleries)} 批次={len(runs)} 分片={total}")
        for index, (g_chunk, r_chunk) in enumerate(payloads, start=1):
            try:
                res = self._request("POST", "/api/remote/sync",
                                    payload={"username": username, "direction": "push",
                                             "galleries": g_chunk, "runs": r_chunk})
            except RemoteError as exc:
                _log(f"推送分片 {index}/{total} 失败 user={username}: {exc} (HTTP {exc.status})")
                raise RemoteError(f"第 {index}/{total} 片推送失败：{exc}", exc.status) from exc
            part = res.get("pushed") or {}
            pushed["galleries"] += int(part.get("galleries") or 0)
            pushed["runs"] += int(part.get("runs") or 0)
        _log(f"推送完成 user={username} 远端合并 作品={pushed['galleries']} "
             f"批次={pushed['runs']} 分片={total}")
        return {"ok": True, "username": username, "pushed": pushed, "chunks": total}


# ============================================================
# 模块级默认实例 + 注入（测试用 fake）
# ============================================================

_client = None


def set_client(client) -> None:
    """注入自定义客户端（测试用）；传 None 恢复默认。"""
    global _client
    _client = client


def reset_client() -> None:
    """丢弃注入的 fake，下次 get_client() 重新按 config 建默认实例。"""
    set_client(None)


def get_client() -> RemoteClient:
    global _client
    if _client is None:
        _client = RemoteClient()
    return _client
