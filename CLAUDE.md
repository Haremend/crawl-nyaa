# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python run.py                      # 启动服务（默认 http://127.0.0.1:8787）
python run.py --open               # 启动并自动打开浏览器
python run.py --seed-demo          # 写入 samples/ 里的离线演示数据，预览前端
python -m unittest discover -s tests -q    # 全量测试（173 项，全部离线，不发真实网络请求）
python -m unittest tests.test_titles -v    # 运行单个测试模块
node --check web/app.js             # 前端语法检查（无构建、无 npm）
```

提交前必须跑 AGENTS.md §9 的残留扫描（非保留域名 / 密钥特征 / 品牌词 grep 三条命令），并确认 `git status` 不含 `data/`、`cover/`、`download/`、`.test-tmp/` 下的文件。

## 文档阅读顺序

项目自带完整的需求与设计文档，改代码前先读：

- `需求基线.md` — 需求语义、边界、验收标准
- `功能清单.md` — 功能完成状态与测试日期
- `详细设计.md` — 实现方案
- `AGENTS.md` — **动手前的硬约束**（§1 硬性约束、§4 业务规则、§7 已知坑、§10 多用户约束必读）
- `.env.example` — 全部可配置项与默认值

AGENTS.md 是权威约束文件，本文件不重复其内容，只做入口指引。

## 硬性约束（摘要，完整规则见 AGENTS.md）

- **不暴露抓取来源**：这是公开仓库。代码、注释、测试、示例数据里不得出现真实站点域名、品牌名、社团 slug、真实作品 ID/标题/作者。示例数据一律用 `example.net` / `SampleCircle` 这类虚构占位。
- **零第三方依赖**：后端只用 Python 标准库（`urllib`/`html.parser`/`sqlite3`/`http.server`），前端原生 HTML/CSS/JS，无 npm 无构建。不要引入 requests/bs4/flask/react。
- **站点地址只能来自三处**：用户输入的列表页 URL（代码从其中派生 origin）、环境变量 `CLAW_API_ORIGIN`、最近一次抓取记录。不得在代码里写死域名。
- **出网统一走 `Fetcher()`**：不要显式传 `proxy=` 或自己拼 `ProxyHandler`，否则会绕过「代理开关」（开关状态存 `settings` 表 `proxy_enabled` 键，关闭时 `Fetcher` 直连）。历史 bug：`Fetcher(proxy=config.PROXY)` 导致关代理后仍走代理报 407。
- **下载走后端转发 + 文件名净化**：上游签名 URL 不得直接给浏览器；落盘文件名一律用 `app/filenames.download_filename()`，格式 `{id}_{名称}.{ext}`。
- **上游接口 Key 规则**：`auth=False`（详情/标签/站点配置）不带 `Authorization` 头；`auth=True`（收藏/下载/账号）才带。回归测试在 `tests/test_downloads.py`。

## 架构地图

| 文件 | 职责 |
| --- | --- |
| `run.py` | 启动入口（参数解析 + `db.init_db()` + 起服务），无业务逻辑 |
| `app/config.py` | 全部 `CLAW_*` 配置；先加载 `.env`（不覆盖已存在环境变量） |
| `app/parser.py` | 列表页 HTML 解析（`HTMLParser` + 正则降级）；语言来自卡片 `lang-xx` class |
| `app/scraper.py` | HTTP 抓取（代理、重试、限速、翻页生成器）；`Fetcher`/`fetch_raw` 全项目复用 |
| `app/detail.py` | 详情 API 客户端（公开接口，不带 Key） |
| `app/upstream.py` | 需要 Key 的上游接口（收藏、下载、账号），auth 规则在此实现 |
| `app/titles.py` | 标题指纹与「是否同一作品」判定，去重核心算法 |
| `app/db.py` | SQLite 存取层（体量最大，~1840 行，改动跑全量测试） |
| `app/service.py` | 抓取任务管理（后台线程、进度、取消），锁必须是 `RLock` |
| `app/covers.py` | 封面下载到 `cover/`，文件名 `<gallery_id>.<ext>` |
| `app/downloader.py` / `app/download_queue.py` | 批量下载服务 + 服务端下载队列（任务存 `download_jobs` 表，N 个 worker 并发） |
| `app/server.py` | 内置 `ThreadingHTTPServer`，静态资源 + 全部 JSON API 路由 |
| `app/supabase_client.py` | 可选云端副本（多用户开发中） |
| `app/demo.py` | 离线演示数据写入，只用 `samples/` |
| `web/` | 原生单页前端（`index.html`/`app.js`/`style.css`），由后端托管 |

## 数据模型要点

- SQLite，WAL 模式，每次操作独立连接。加字段必须同时改 `SCHEMA`（新库）与 `MIGRATIONS`（老库升级），迁移必须幂等。
- `upsert_items()` 对已存在的 `gallery_id` **一律跳过、不覆盖**，只刷新 `last_seen`；返回值 `new_ids` 才是本次新增，详情补全/封面下载只处理这些。
- `hidden` 是逻辑删除：所有展示/下载查询必须带 `hidden = 0`；隐藏记录仍参与「已收录」去重判定，重抓不会重新插入。
- 多用户（开发中）：登录用户库在 `data/users/<安全用户名>.sqlite3`，匿名模式用默认库 `data/gallery.sqlite3`。

## 选版/排序规则（改动需谨慎）

- 选版优先级（同一作品内部）：无码 > 日语 > 中文 > 英文 > 其他；同语言页数多优先；再同级新上传优先。
- 列表排序（不同作品之间，`sort=priority`）：按语言优先级排队，同语言**新作排前面**（不比页数，与选版规则不同）。
- 去重三层：`gallery_id` 主键 → 详情原始标题归一化精确匹配 → 列表标题指纹保守模糊合并。关键守卫：卷号签名（`Vol.2` vs `Vol.3`，相似度高达 0.988）必须区分开，见 `tests/test_titles.py`。

## 已知坑（真实踩过，别重犯）

1. `ScrapeService` 的锁必须是 `RLock`（`start()` 持锁期间会调 `_log()`，普通 `Lock` 自我死锁）。
2. `Fetcher.fetch_raw()` 不能删，`fetch_text()`/`fetch_bytes()` 都依赖它。
3. 详情 API 的 `thumbnail` 是对象（`{"path": ..., ...}`），必须用 `_thumbnail_url()` 归一化，直接 `str()` 会破坏列表页的完整 URL 导致封面全失败。
4. 原始标题与列表标题不能混用做指纹；精确分组用 `original_key`，模糊合并一律用列表标题指纹。
5. 增量抓取停止条件是本页 `dup > 0`（任意一条已收录即停），不是「整页重复」。
6. 测试里 `apply_details` 只能更新已存在的行，要先 `upsert_items` 再 `apply_details`。
7. `merge_runs_for_url()` 会改写旧条目的 `batch_id`，不是单纯删批次。

## 测试约定

- 测试不得访问外网；需要 HTTP 的场景用 `FakeFetcher` 之类的桩（参考 `tests/test_downloads.py`）。
- 临时目录用项目内 `.test-tmp/`，**不要用 `tempfile.TemporaryDirectory`**（本机环境下创建 0o700 目录会导致后续无法访问），参考 `tests/` 里 `make_temp_dir()` 的普通 `mkdir` + `shutil.rmtree` 方式。
- 修 bug 先补一条能复现的测试再改代码。

## 前端约定

- 浅色主题：白底黑字，基准字号 16px，卡片标题 17px。
- 卡片统一由 `cardHtml()` 渲染；所有动态文本必须经过 `esc()`（防 XSS）；所有请求统一走 `api()` 包装函数（自动附 `X-API-Key`，401 自动弹门禁）。
