# AGENTS.md

> 给在本仓库工作的 AI Agent（以及人类协作者）的项目约束与说明。
> **动手改代码之前先把本文读完**，尤其是「1. 硬性约束」和「7. 已知的坑」。

---

## 0. 项目是什么

一个**本地画廊列表抓取与整理工具**：

- 抓取列表页 → 存 SQLite → 同作品去重 → 按自定义优先级排序 → 标记待下载 / 已下载 → 用 API Key 调站点接口下载
- 内置一个零构建的原生 Web 前端（浅色主题），后端只用 Python 标准库
- 入口：`run.py`（启动本地 HTTP 服务，默认 `http://127.0.0.1:8787`）

---

## 1. 硬性约束（不可违反）

### 1.1 不暴露抓取来源 —— 最高优先级

本仓库是**公开仓库**，而项目的定位是「不让人知道抓的是哪个站点」。因此：

- ❌ 代码、注释、docstring、README、前端文案、测试、示例数据里**不得出现**真实站点域名、品牌名、
  或任何能反查出站点的标识（社团 slug、真实作品 ID、真实作品标题、真实作者名都算）
- ✅ 站点地址只能来自两处：
  1. 用户在界面输入的**列表页 URL**（代码从中派生 origin）
  2. 环境变量 `CLAW_API_ORIGIN`
- ✅ 示例 / 测试数据**必须是虚构的**（统一用 `example.net`、`SampleCircle`、`Sample Anthology`
  这类占位内容）
- ✅ 提交前必须跑残留扫描（见 §9）

> 历史教训：项目早期的测试样本里带过真实社团 slug 与真实作品标题，一搜就能定位来源站点。
> 这类内容一旦进入 git 历史就**永久可查**，所以必须在**首次提交前**清理干净。

### 1.2 绝不提交数据与凭据

- `data/`（SQLite）、`cover/`（封面）、`download/`（下载产物）、`.test-tmp/` 已在 `.gitignore` 中，
  **不要移除这些规则，也不要用 `git add -f` 绕过**
- ❌ **API Key、cookie、session token 绝不写入**：代码、配置文件、日志、数据库、测试、提交
- ✅ Key 只允许在**单次请求 / 单个任务的生命周期**内存在（参考 `app/upstream.py` 的设计）
- ✅ 接收 Key 的接口只从请求头 `X-API-Key`（或 `?apikey=`）读取，**用完即弃**

### 1.3 零第三方依赖

- 后端：**只用 Python 标准库**（`urllib` / `html.parser` / `sqlite3` / `http.server`）
  —— 不要引入 requests / beautifulsoup / fastapi / flask
- 前端：**原生 HTML/CSS/JS，无 npm、无构建步骤** —— 不要引入 React/Vue/打包器
- 唯一例外：测试与开发辅助可以按需使用，但**运行时不依赖**

### 1.4 代理开关要作用在「后端出网」

- `Fetcher` 默认走 `config.effective_proxy()`：代理开关关闭时返回空字符串 → **直连**
- 开关状态存在 `settings` 表（键 `proxy_enabled`），服务启动时自动恢复
- 新增任何出网代码时，都要用 `Fetcher()`（**不要显式传 `proxy=`**，也不要自己拼 `ProxyHandler`），否则会绕开这个开关
- 历史 bug：`ScrapeService` 里写成 `Fetcher(proxy=config.PROXY)`，导致页面关掉代理后抓取仍走代理并报 407；
  回归测试在 `tests/test_proxy.py::ScrapeProxyTests`

### 1.5 站点地址只能来自这三处

代码里**不得写入固定域名**。站点地址按以下顺序解析（`db.get_site_origin()`）：

1. 前端门禁里填写的域名（`settings.site_origin`）
2. 环境变量 `CLAW_API_ORIGIN`
3. 最近一次抓取的列表页 URL / 库内作品链接推导

抓取时会把推导出的地址同步回设置，保证与前端填写的一致。

### 1.6 下载必须走后端转发，文件名必须净化

- **不要把上游的签名下载地址直接交给浏览器**：签名与取签名时的出口 IP/会话绑定，
  后端走代理时浏览器直连会 `Invalid signature`；跨域地址上 `download` 属性也会被忽略（变成页面跳转）。
  统一下载入口：`GET /api/download/file?gallery_id=..&format=..&apikey=..`（同源 + `attachment`）
- **落盘文件名一律用 `app.filenames.download_filename()`**，格式 `{id}_{名称}.{ext}`：
  它会处理非法字符、结尾点/空格、Windows 保留名、长度上限与空标题兜底。
  不要自己拼文件名。

### 1.7 配置走 .env，不要硬编码

- 所有可调参数都从环境变量读取（`app/config.py`），并同步补到 `.env.example`（**入库存模板**）
- `.env` 是本地文件、**已被 gitignore**，不要提交，也不要把密钥写进去
- 启动时 `app/config.py` 会先加载 `.env`，且**不覆盖已存在的环境变量**（命令行覆盖优先）

### 1.8 立即下载的两种模式

`CLAW_DOWNLOAD_MODE` 控制 `/api/download/file` 的取文件方式，两条路径都要维护：

| 模式 | 行为 | 注意 |
| --- | --- | --- |
| `cache` | 先下载到 `download/`，再读盘转发 | **必须先查缓存再请求上游签名**，否则「重复下载不请求上游」就失效；先写 `.part` 再原子改名；并发用 `_cache_lock()` |
| `stream` | 64KB 边读边转发，不落盘 | 不写磁盘；`Content-Length` 缺失时靠关闭连接告知结束 |

### 1.9 下载统一走服务端队列

- 所有下载都通过 `app/download_queue.py` 的 `DownloadQueue`：任务写进 `download_jobs` 表，
  N 个 worker 并发消费（N = `CLAW_DOWNLOAD_CONCURRENCY`，默认 5）
- 队列在数据库里 → 任何标签页看到的是同一份；**不要把进度只放在某个请求的内存里**
- 进度写库有节流（`PROGRESS_INTERVAL`），避免每个 64KB chunk 都写一次 SQLite
- **API Key 只在队列进程的内存里**（`_keys`），不写数据库；服务重启后未完成任务会失败，需重新入队
- 入队用 `create_job()`：同一作品 + 同一格式的排队/运行中任务会自动去重
- 批次来源的任务（`source="batch"`）落到 `download/batch_<id>/` 并写 `manifest.json` / `works.csv`

### 1.10 上游接口的 Key 使用规则

用户明确要求：**上游接口不需要 Key 的就不要带 Key。**

- `auth=False`（公开接口：详情、标签查询、站点配置）→ **不带** `Authorization` 头
- `auth=True`（收藏列表、下载、账号信息）→ 才带 `Authorization: Key <用户 Key>`
- 这条规则有专门的回归测试锁定（`tests/test_downloads.py`），改动 `upstream.py` 时不要破坏

---

## 2. 架构地图

| 文件 | 职责 | 注意 |
| --- | --- | --- |
| `run.py` | 启动入口（参数解析 + 起服务） | 无业务逻辑 |
| `app/config.py` | 全部配置与环境变量（`CLAW_*`） | 域名**不写死**在这里 |
| `app/parser.py` | 列表页 HTML 解析（`HTMLParser` + 正则降级） | 语言来自卡片 `lang-xx` class |
| `app/scraper.py` | HTTP 抓取（代理、重试、限速、翻页生成器） | `fetch_raw` / `request` 供全项目复用 |
| `app/detail.py` | 详情 API 客户端（原始标题、标签、页数） | 公开接口，**不带 Key** |
| `app/upstream.py` | 需要 Key 的上游接口（收藏、下载、账号） | §1.4 的 Key 规则在这里实现 |
| `app/titles.py` | 标题指纹与「是否同一作品」判定 | 去重的核心算法 |
| `app/db.py` | SQLite 存取层（入库、去重聚类、查询、迁移） | 体量最大，改动要跑全量测试 |
| `app/service.py` | 抓取任务管理（后台线程、进度、取消） | 锁必须是 `RLock` |
| `app/covers.py` | 封面下载到 `cover/` | 文件名 = `<gallery_id>.<ext>` |
| `app/downloader.py` | 批量下载服务（待下载 / 整批保存） | 支持服务器保存与清单写入 |
| `app/server.py` | 内置 HTTP 服务（静态资源 + JSON API） | 路由与 Key 读取都在这里 |
| `app/demo.py` | 演示数据（离线预览前端） | 只用 `samples/` 里的虚构样本 |
| `web/` | 前端（`index.html` / `app.js` / `style.css`） | 无构建，直接由后端托管 |

---

## 3. 数据模型与迁移

- SQLite，`WAL` 模式；每次操作**独立连接**（配合线程使用）
- **加字段必须同时改两处**：`SCHEMA`（新库）与 `MIGRATIONS`（老库升级）
  —— 迁移必须是**幂等**的，老库升级后数据不能丢
- 语义约束：
  - **已存在记录一律跳过、不覆盖**：`upsert_items()` 对库里已有的 `gallery_id` 不写入任何内容字段
    （无论本次数据是否变化），只刷新 `last_seen`；返回值里的 `new_ids` 才是本次真正新增的 ID，
    详情补全 / 封面下载只处理 `new_ids`
  - `hidden` 是逻辑删除：所有展示 / 下载查询必须带 `hidden = 0`；重复抓到时同样算「已存在」而跳过
  - `want_download` / `downloaded` 是**作品组级**标记（组内任一版本被标记即整组生效），
    但标记本身只落在**推荐版本**上
  - `batch_id` 记录归属批次；同一 URL 抓取成功后只保留最新批次（旧批次条目改写到新批次）

---

## 4. 业务规则（核心，改动需谨慎）

### 4.1 选版优先级（同一作品内部，选哪个版本做代表）

1. **无码** > **日语** > **中文** > **英文** > 其他语言
2. 同语言：**页数多**优先
3. 再同级：**新上传**优先（用详情 API 的 `upload_date`）

### 4.2 列表排序（不同作品之间，`sort=priority`）

1. 先按 **无码 > 日语 > 中文 > 英文 > 其他** 排队
2. 同语言：**新作排前面**（⚠️ 这里**不比页数**，与 4.1 不同）

### 4.3 同作品去重（三层）

1. `gallery_id` 主键 —— 幂等
2. 详情 API 的**原始标题**归一化后精确匹配 —— 同一作品的不同语言版本共享原文标题
3. 列表标题**指纹**保守模糊合并 —— 无详情时的兜底

> ⚠️ 关键守卫：**卷号签名（`Vol.2` vs `Vol.3`）必须不同**。这两个标题的字符串相似度高达 0.988，
> 只靠相似度一定会误合并。相关回归测试见 `tests/test_titles.py`、`tests/test_titles_dataset.py`。

### 4.4 增量抓取

- 同 URL 有历史成功批次且 `incremental` 开启时：逐页抓取，**本页出现任意已收录条目（`dup > 0`）即停止翻页**
- 前提：上游列表按时间倒序；不满足时用 `CLAW_INCREMENTAL=0` 退化为完整抓取
- 隐藏作品也算「已收录」，会触发停止并跳过
- 详情补全 / 封面下载只处理本页 `new_ids`，不重复请求旧数据

### 4.5 批次合并与空批次

- `items_new == 0` 的 done 批次立即删除，不留空批次号
- `items_new > 0` 时调用 `merge_runs_for_url()`：旧批次条目 `batch_id` 改写为新批次，再删除旧批次记录
- 「保存全部（当前批次）」依赖这一点，才能拿到该 URL 的最新完整清单

### 4.6 隐藏（逻辑删除）

- 卡片隐藏按钮提交**合并卡全部变体 ID**；接口 `POST /api/hidden`
- 隐藏后：前端不展示、待下载 / 批次保存目标排除、再次抓取到直接跳过
- 恢复：调用同一接口传 `hidden:false`（界面暂未提供恢复入口）

### 4.7 收藏同步

- `POST /api/favorites/sync`：分页拉取站点收藏 → `upsert_items`（已存在不覆盖）→ `site_favorite=1` + `want_download=1`
- 本地「待下载」与站点收藏是两套独立标记，不要混用

---

## 5. 前端约定

- **浅色主题**：白底黑字，基准字号 16px，卡片标题 17px（用户明确要求过）
- 卡片统一由 `cardHtml()` 渲染，两个 tab（作品库 / 我的收藏）共用同一套模板与排序规则
- **所有动态文本必须经过 `esc()`**（标题、标签来自外部数据，直接拼 HTML 会有 XSS 风险）
- 所有请求统一走 `api()` 包装函数（自动附带 `X-API-Key`，401 时自动弹门禁）
- 卡片左上角角标：已下载（绿）优先于待下载（黄）

---

## 6. 测试与验证

```bash
# 全量测试（173 项，全部离线，不发真实网络请求）
python -m unittest discover -s tests

# 前端语法
node --check web/app.js
```

约定：

- 测试**不得访问外网**；需要 HTTP 的场景用 `FakeFetcher` 之类的桩（参考 `tests/test_downloads.py`）
- 测试临时目录统一用项目内 `.test-tmp/`：
  ⚠️ **不要用 `tempfile.TemporaryDirectory`** —— 在本机环境它创建 0o700 目录会导致后续无法访问，
  请用 `tests/` 里 `make_temp_dir()` 那种方式（普通 `mkdir` + `shutil.rmtree`）
- 修 bug 时**先补一条能复现的测试**，再改代码
- 新功能回归见 `tests/test_new_features.py`（隐藏 / 增量 / 空批次 / 收藏同步）；
  批次合并与「保留原始记录」见 `tests/test_batches.py`

---

## 7. 已知的坑（都真实踩过，别重犯）

1. **`ScrapeService` 的锁必须是 `RLock`**
   `start()` 持锁期间会调用 `_log()`，普通 `Lock` 会自我死锁 → 任务永远不启动。
   （回归测试：`tests/test_service.py::test_start_returns_quickly_with_reentrant_lock`）
2. **`Fetcher.fetch_raw()` 不能删**
   `fetch_text()` / `fetch_bytes()` 都依赖它。重构 `Fetcher` 时注意保留这个薄封装。
3. **详情 API 的 `thumbnail` 是对象**
   `{"path": "galleries/<media>/thumb.webp", ...}`，直接 `str()` 会得到脏字符串并覆盖列表页的完整 URL，
   导致封面下载**全部失败**。必须用 `_thumbnail_url()` 归一化。
4. **原始标题与列表标题不能混用做指纹**
   详情补全后两者核心文本完全不同（一个日文一个英文），混用会让合并失效。
   精确分组用 `original_key`，模糊合并一律用**列表标题**的指纹。
5. **列表页语言在 class 上**
   现代列表页没有 `/language/` 标签锚点，语言在 `<div class="gallery lang-gb">` 上（gb=English、jp=Japanese、cn=Chinese）。
6. **下载链接是短时效签名 URL**
   过期时间约几分钟，取到后要尽快使用；`torrent` 格式限流很严（5/分钟/IP）。
7. **测试里的 `apply_details` 只能更新已存在的行**
   它不会插入新记录；测试里要先 `upsert_items` 再 `apply_details`。
8. **增量停止条件是 `dup > 0` 而不是「整页重复」**
   列表首屏常混入置顶 / 更新条目；只要出现一条已收录，按时间倒序后续只会更旧。
9. **`merge_runs_for_url()` 会改写旧条目的 `batch_id`**
   不要把它当成单纯删除批次；测试批次过滤时要意识到条目已归属到最新批次。
10. **隐藏记录仍参与「已收录」判定**
    隐藏只是展示 / 下载层面过滤，去重判定必须带上隐藏行，否则重抓会把它重新插入。

---

## 8. 常用命令

```bash
python run.py                 # 启动服务（默认 127.0.0.1:8787）
python run.py --open          # 启动并打开浏览器
python run.py --seed-demo     # 写入离线演示数据（虚构样本）
python -m unittest discover -s tests
node --check web/app.js
```

---

## 9. 提交前检查清单

- [ ] `python -m unittest discover -s tests` 全绿
- [ ] `node --check web/app.js` 通过
- [ ] 残留扫描无命中（非保留域名 / 密钥特征 / 品牌词）：

```bash
# 1) 不应出现非保留域名（只允许 example.net / localhost / 127.0.0.1）
grep -rnoE 'https?://[A-Za-z0-9.-]+' --include='*.py' --include='*.js' \
  --include='*.html' --include='*.css' --include='*.md' --include='*.json' \
  --exclude-dir={data,cover,download,.test-tmp,.git} . \
  | grep -viE 'example\.(net|com|org)|localhost|127\.0\.0\.1' \
  || echo "无非保留域名"

# 2) 不应出现密钥样式字符串（<前缀>_<长随机串>）
grep -rnoE '[A-Za-z]{2,6}_[A-Za-z0-9_-]{24,}' --include='*.py' --include='*.js' \
  --include='*.md' --include='*.json' \
  --exclude-dir={data,cover,download,.test-tmp,.git} . \
  || echo "无密钥样式字符串"

# 3) 品牌词自查：用你已知的来源站点标识去搜（本文件不写出来，避免自曝）
#    grep -rni '<来源站点标识>' --exclude-dir={data,cover,download,.test-tmp,.git} .
```

- [ ] `git status` 里**没有** `data/` `cover/` `download/` 下的文件
- [ ] 没有把 Key / cookie 写进任何被提交的文件
- [ ] 涉及站点地址的代码仍然只从「用户输入 URL」或 `CLAW_API_ORIGIN` 取值