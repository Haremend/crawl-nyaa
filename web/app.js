"use strict";

/* ==================================================================
 * 状态
 * ================================================================== */
const state = {
  key: "",
  user: null,            // null=未登录；{username, has_api_key, site_origin}
  authTab: "login",      // login / register
  tab: "library",
  q: "",
  languages: [],
  dedupe: true,
  uncensored: false,
  wantOnly: false,
  downloadedOnly: false,
  sort: "priority",
  page: 1,
  perPage: 48,
  data: null,
  meta: null,
  saveMode: "server",
  proxyEnabled: true,
  localCancel: false,
  localRunning: false,
  batchId: "",
  batches: [],
  batchSearch: "",
  favPage: 1,
  favQuery: "",
  favData: null,
  scrapePolling: null,
  dlPolling: null,
  finishedRunId: null,
  urlInitialized: false,
};

const $ = (id) => document.getElementById(id);

/* ==================================================================
 * API Key：cookie 存取 + 门禁
 * ================================================================== */
const KEY_COOKIE = "claw_api_key";
const SITE_COOKIE = "claw_site_origin";

function readCookie(name) {
  const target = name + "=";
  for (const part of document.cookie.split(";")) {
    const item = part.trim();
    if (item.startsWith(target)) return decodeURIComponent(item.slice(target.length));
  }
  return "";
}

function writeCookie(name, value, days = 365) {
  const maxAge = Math.floor(days * 86400);
  document.cookie = name + "=" + encodeURIComponent(value)
    + "; path=/; max-age=" + maxAge + "; SameSite=Lax";
}

function deleteCookie(name) {
  document.cookie = name + "=; path=/; max-age=0; SameSite=Lax";
}

/** 初始化 Key：URL 参数 > cookie；URL 参数写入 cookie 后从地址栏移除。 */
function initApiKey() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = (params.get("apikey") || "").trim();
  const domainFromUrl = (params.get("domain") || "").trim();
  if (domainFromUrl) {
    writeCookie(SITE_COOKIE, domainFromUrl);
    params.delete("domain");
  }
  if (fromUrl) {
    writeCookie(KEY_COOKIE, fromUrl);
    params.delete("apikey");
    const query = params.toString();
    const clean = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
    window.history.replaceState(null, "", clean);   // 避免 Key 留在历史记录里
  }
  state.key = fromUrl || readCookie(KEY_COOKIE);
}

/** 显示「立即下载」当前是缓存模式还是流式模式（由服务端 .env 决定）。 */
function renderDownloadMode(mode) {
  const box = $("modeHint");
  if (!box) return;
  if (mode === "cache") {
    box.textContent = "立即下载：缓存模式（先存服务器磁盘再转发，重复下载不再请求上游）";
  } else {
    box.textContent = "立即下载：流式模式（64KB 边下边转，不占服务器磁盘）";
  }
}

function renderSiteChip(origin) {
  const value = (origin || "").trim();
  $("siteChip").textContent = value ? ("站点 " + value.replace(/^https?:\/\//, "")) : "站点 未设置";
  $("siteChip").title = value || "尚未设置站点域名";
}

function prefillGate(meta) {
  const origin = (meta && meta.site_origin) || readCookie(SITE_COOKIE) || "";
  $("gateDomain").value = origin;
  $("gateInput").value = "";
  if (origin) { $("gateInput").focus(); } else { $("gateDomain").focus(); }
}

function showGate(message) {
  $("gate").hidden = false;
  $("gateHint").textContent = message || "也可以用 ?apikey=你的KEY 直接进入（进入后会自动从地址栏移除）。";
  $("gateInput").value = "";
  $("gateInput").focus();
}

function hideGate() {
  $("gate").hidden = true;
}

/**
 * 保存 API Key 并进入。
 *
 * 注意：这里**不做任何校验**。全新部署时还没有任何站点地址，
 * 任何需要站点信息的接口都会失败（例如 /api/site-favorites 会返回
 * 「未知站点地址」），如果在这里校验就会导致第一次根本进不去。
 * Key 是否正确，等真正用到（收藏列表 / 下载）时再报错。
 */
async function submitGate() {
  const domain = ($("gateDomain").value || "").trim();
  const key = ($("gateInput").value || "").trim();
  if (!domain) { toast("请输入站点域名", "error"); $("gateDomain").focus(); return; }
  if (!key) { toast("请输入 API Key", "error"); $("gateInput").focus(); return; }

  $("gateBtn").disabled = true;
  // 1) 域名保存到服务端（后端访问站点接口要用），只做格式校验
  try {
    const data = await api("/api/settings/site", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin: domain }),
    });
    state.siteOrigin = data.site_origin || "";
    writeCookie(SITE_COOKIE, state.siteOrigin);
    renderSiteChip(state.siteOrigin);
  } catch (error) {
    toast("域名无效：" + error.message, "error");
    $("gateHint").textContent = "域名校验失败：" + error.message;
    $("gateBtn").disabled = false;
    return;
  }

  // 2) API Key 只存浏览器，不在这里校验（此时可能还没抓过任何数据）
  state.key = key;
  writeCookie(KEY_COOKIE, key);
  hideGate();
  toast("已保存域名与 API Key", "ok");
  try {
    await reloadAll();
  } catch (error) {
    $("gateHint").textContent = "提示：" + error.message;
  } finally {
    $("gateBtn").disabled = false;
  }
}

/* ==================================================================
 * 认证：登录 / 注册 / 退出 / 用户设置 / 数据同步
 * ==================================================================
 * 登录后站点域名与 API Key 跟账号走（后端从会话用户读取，不回显完整 Key）；
 * 匿名模式仍用浏览器 cookie 里的 Key + 门禁兼容路径。
 */
async function loadAuth() {
  try {
    const data = await api("/api/auth/me", { silent: true });
    state.user = data.logged_in ? data.user : null;
  } catch (error) {
    state.user = null;
  }
  renderAuth();
}

function renderAuth() {
  const area = $("authArea");
  if (state.user) {
    area.innerHTML =
      '<span class="user-name" title="当前登录用户">' + esc(state.user.username) + "</span>"
      + '<span class="auth-divider"></span>'
      + '<button class="btn ghost" id="settingsBtn" title="站点域名 / API Key / 数据同步">设置</button>'
      + '<button class="btn ghost" id="syncBtnTop">同步</button>'
      + '<button class="btn ghost" id="logoutBtn">退出</button>';
    $("settingsBtn").addEventListener("click", openSettingsModal);
    $("syncBtnTop").addEventListener("click", openSettingsModal);
    $("logoutBtn").addEventListener("click", logout);
    hideGate();
  } else {
    area.innerHTML = '<button class="btn primary" id="loginBtn">登录 / 注册</button>';
    $("loginBtn").addEventListener("click", () => openAuthModal("login"));
  }
}

function openAuthModal(tab) {
  state.authTab = tab || "login";
  $("authModal").hidden = false;
  document.querySelectorAll(".modal-tab").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.authTab === state.authTab));
  $("authSubmit").textContent = state.authTab === "login" ? "登录" : "注册";
  $("authExtra").hidden = state.authTab !== "register";
  $("authHint").textContent = state.authTab === "login"
    ? "本地无此用户且已配置云端时，会自动从云端拉取账号验证。"
    : "注册后你的账号、站点域名与 API Key 可在云端跨设备同步。";
  $("authUsername").value = "";
  $("authPassword").value = "";
  $("authSite").value = "";
  $("authKey").value = "";
  $("authUsername").focus();
}

function closeAuthModal() { $("authModal").hidden = true; }

async function submitAuth() {
  const username = $("authUsername").value.trim();
  const password = $("authPassword").value;
  if (!username || !password) { toast("请填写用户名和密码", "error"); return; }
  const body = { username, password };
  const path = state.authTab === "login" ? "/api/auth/login" : "/api/auth/register";
  if (state.authTab === "register") {
    body.site_origin = $("authSite").value.trim();
    body.api_key = $("authKey").value.trim();
  }
  $("authSubmit").disabled = true;
  try {
    await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    toast(state.authTab === "login" ? "登录成功" : "注册成功", "ok");
    closeAuthModal();
    state.key = "";  // 登录后不再依赖浏览器 Key
    deleteCookie(KEY_COOKIE);
    await loadAuth();
    await reloadAll();
  } catch (error) {
    toast(error.message, "error");
    $("authHint").textContent = error.message;
  } finally {
    $("authSubmit").disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast("已退出登录");
    state.user = null;
    state.key = "";
    renderAuth();
    await loadMeta().catch(() => {});
    if (!state.key) {
      prefillGate(state.meta);
      showGate();
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

function openSettingsModal() {
  if (!state.user) { openAuthModal("login"); return; }
  $("settingsModal").hidden = false;
  $("settingsUsername").textContent = state.user.username;
  $("settingsSite").value = state.user.site_origin || "";
  $("settingsKey").value = "";
  $("syncResult").textContent = "";
  loadSyncStatus();
}

function closeSettingsModal() { $("settingsModal").hidden = true; }

async function saveSettings() {
  const site = $("settingsSite").value.trim();
  const key = $("settingsKey").value.trim();
  const body = { site_origin: site };
  if (key) body.api_key = key;
  $("settingsSave").disabled = true;
  try {
    const data = await api("/api/settings/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.user.has_api_key = Boolean(data.has_api_key);
    state.user.site_origin = data.site_origin || "";
    const note = data.cloud_pushed ? "（已同步到云端）" : (data.cloud_error ? "（云端未同步：" + data.cloud_error + "）" : "");
    toast("已保存" + note, "ok");
    await loadMeta().catch(() => {});
  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("settingsSave").disabled = false;
  }
}

async function loadSyncStatus() {
  try {
    const data = await api("/api/sync/status", { silent: true });
    const text = !data.configured
      ? "未配置云端：在 .env 里填 CLAW_SUPABASE_PROJECT_REF 与 CLAW_SUPABASE_TOKEN 后可跨部署同步。"
      : (data.available ? "云端已就绪，可同步作品与批次记录。" : "云端已配置但表尚未就绪，点「立即同步」会自动建表。");
    $("syncStatusText").textContent = text;
    $("syncBtn").disabled = !data.configured;
  } catch (error) {
    $("syncStatusText").textContent = "无法读取云端状态：" + error.message;
    $("syncBtn").disabled = true;
  }
}

async function doSync() {
  if (!state.user) { toast("请先登录", "error"); return; }
  $("syncBtn").disabled = true;
  $("syncResult").textContent = "正在同步…";
  try {
    const data = await api("/api/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const cloud = data.cloud || {};
    const pulled = data.pulled || {};
    const pushed = data.pushed || {};
    const local = data.local || {};
    $("syncResult").textContent =
      "云端 作品 " + (cloud.galleries || 0) + " / 批次 " + (cloud.runs || 0)
      + " · 拉取合并 作品 " + (pulled.galleries || 0) + " / 批次 " + (pulled.runs || 0)
      + " · 推回 作品 " + (pushed.galleries || 0) + " / 批次 " + (pushed.runs || 0)
      + " · 本地 作品 " + (local.galleries || 0) + " / 批次 " + (local.runs || 0);
    toast("同步完成", "ok");
    await reloadAll();
  } catch (error) {
    $("syncResult").textContent = "同步失败：" + error.message;
    toast("同步失败：" + error.message, "error");
  } finally {
    $("syncBtn").disabled = false;
  }
}

/* ==================================================================
 * 基础工具
 * ================================================================== */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("zh-CN", { hour12: false });
}

function fmtDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso).slice(0, 10) : date.toLocaleDateString("zh-CN");
}

/* ---------- 全局加载遮罩 ----------
 * 用计数处理并发请求；请求很快时（<120ms）不显示，避免闪烁。
 */
let loadingCount = 0;
let loadingTimer = null;
let loadingShownAt = 0;

function showLoading(text) {
  loadingCount += 1;
  if (loadingTimer || !$("loadingOverlay").hidden) return;
  loadingTimer = window.setTimeout(() => {
    loadingTimer = null;
    if (loadingCount <= 0) return;
    $("loadingText").textContent = text || "加载中…";
    $("loadingOverlay").hidden = false;
    loadingShownAt = Date.now();
  }, 120);
}

function hideLoading() {
  loadingCount = Math.max(0, loadingCount - 1);
  if (loadingCount > 0) return;
  if (loadingTimer) { window.clearTimeout(loadingTimer); loadingTimer = null; }
  const shownFor = Date.now() - loadingShownAt;
  const finish = () => { $("loadingOverlay").hidden = true; };
  if (!$("loadingOverlay").hidden && shownFor < 250) {
    window.setTimeout(finish, 250 - shownFor);
  } else {
    finish();
  }
}

async function api(path, options = {}) {
  const opts = Object.assign({}, options);
  opts.headers = Object.assign({}, options.headers || {});
  if (state.key) opts.headers["X-API-Key"] = state.key;
  const silent = Boolean(options.silent);
  if (!silent) showLoading(options.loadingText || "加载中…");
  let response;
  try {
    response = await fetch(path, opts);
  } finally {
    if (!silent) hideLoading();
  }
  let data = {};
  try { data = await response.json(); } catch (error) { /* 非 JSON */ }
  if (response.status === 401) {
    // 登录用户的 401 = 保存的 Key 缺失/失效，引导去设置；匿名用户才弹门禁
    if (state.user) {
      toast("需要 API Key：请在设置里填写站点域名与 Key", "error");
      openSettingsModal();
    } else {
      showGate("需要有效的 API Key 才能继续。");
    }
    throw new Error(data.error || "需要 API Key");
  }
  if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
  return data;
}

let toastTimer = null;
function toast(message, kind = "info") {
  const box = $("toast");
  box.textContent = message;
  box.className = "toast " + kind;
  box.hidden = false;
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { box.hidden = true; }, 4600);
}

function buildParams(includePaging = true) {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  state.languages.forEach((lang) => params.append("lang", lang));
  params.set("dedupe", state.dedupe ? "1" : "0");
  if (state.uncensored) params.set("uncensored", "1");
  if (state.wantOnly) params.set("want", "1");
  if (state.downloadedOnly) params.set("downloaded", "1");
  if (state.batchId) params.set("batch", state.batchId);
  params.set("sort", state.sort);
  if (includePaging) {
    params.set("page", String(state.page));
    params.set("per_page", String(state.perPage));
  }
  return params;
}

/* ==================================================================
 * 元数据
 * ================================================================== */
async function loadMeta() {
  const meta = await api("/api/meta");
  state.meta = meta;
  if (!state.urlInitialized && meta.default_url) {
    $("urlInput").value = meta.default_url;
    state.urlInitialized = true;
  }
  renderProxy(meta);
  const parts = ["库内 " + meta.stats.entries + " 条", "去重后 " + meta.stats.works + " 部"];
  if (meta.stats.want_download) parts.push("待下载 " + meta.stats.want_download);
  if (meta.stats.downloaded) parts.push("已下载 " + meta.stats.downloaded);
  $("dbChip").textContent = parts.join(" / ");
  $("dbChip").title = meta.db_path;
  if (meta.download_dir) $("dlDirHint").textContent = meta.download_dir;
  renderLastRun(meta.stats.last_run);
  renderDownloadSummary(meta.download_status);
  renderSiteChip(meta.site_origin);
  renderDownloadMode(meta.download_mode);
  state.batches = meta.batches || [];
  renderBatchOptions();
  renderSetupHint(meta);
  if (meta.scrape && meta.scrape.running) startPolling();
  else if (meta.scrape && ["done", "cancelled", "error"].includes(meta.scrape.status)) renderProgress(meta.scrape);
}

function renderLastRun(run) {
  const chip = $("lastRunChip");
  if (!run) { chip.textContent = "暂无抓取记录"; chip.title = ""; return; }
  const statusText = { done: "成功", error: "失败", cancelled: "已取消", running: "进行中" }[run.status] || run.status;
  chip.textContent = "上次抓取 " + fmtTime(run.finished_at || run.started_at) + " · " + statusText;
  chip.title = run.url || "";
}

function renderDownloadSummary(status) {
  if (!status) { $("dlSummary").textContent = ""; return; }
  const map = { idle: "空闲", running: "下载中", done: "已完成", cancelled: "已取消", error: "失败" };
  const text = "状态：" + (map[status.status] || status.status)
    + " · 成功 " + (status.done || 0) + " / 共 " + (status.total || 0)
    + ((status.failed || 0) ? " · 失败 " + status.failed : "");
  $("dlSummary").textContent = text;
}

/* ==================================================================
 * 批次号（顶部下拉搜索）
 * ================================================================== */
function batchLabel(batch) {
  const when = fmtTime(batch.finished_at || batch.started_at || "").slice(0, 16);
  const url = batch.url || "";
  return "#" + batch.run_id + " · " + (batch.works || 0) + " 部 · " + when + " · " + url;
}

function renderBatchOptions() {
  const select = $("batchSelect");
  const keyword = (state.batchSearch || "").toLowerCase();
  const options = ['<option value="">全部批次</option>'];
  for (const batch of state.batches) {
    const label = batchLabel(batch);
    if (keyword && !label.toLowerCase().includes(keyword)) continue;
    const selected = String(batch.run_id) === String(state.batchId) ? " selected" : "";
    options.push('<option value="' + batch.run_id + '"' + selected + ">" + esc(label) + "</option>");
  }
  select.innerHTML = options.join("");
  renderBatchInfo();
}

/** 首次部署引导：还没有站点地址时，告诉用户先做什么。 */
function renderSetupHint(meta) {
  const box = $("setupHint");
  if (!meta) { box.hidden = true; return; }
  const hasSite = Boolean(meta.site_origin);
  const hasData = (meta.stats && meta.stats.entries > 0) || (meta.batches || []).length > 0;
  if (!hasSite && !hasData) {
    box.innerHTML = "首次使用：把要抓的<strong>列表页 URL</strong> 粘到上面的输入框，点击「开始抓取」。"
      + "抓取一次后才会知道站点地址，届时「我的收藏」与下载功能即可使用。";
    box.hidden = false;
  } else if (!hasSite) {
    box.innerHTML = "还没有站点地址：再抓取一次列表页即可（收藏与下载需要它）。";
    box.hidden = false;
  } else {
    box.hidden = true;
  }
}

function renderProxy(meta) {
  const enabled = meta ? Boolean(meta.proxy_enabled) : true;
  const address = (meta && (meta.proxy_address || meta.proxy)) || "";
  state.proxyEnabled = enabled;
  $("proxyChip").textContent = enabled ? ("代理 " + (address || "已开启")) : "直连（不使用代理）";
  $("proxyBtn").textContent = enabled ? "代理：开" : "代理：关";
  $("proxyBtn").title = enabled
    ? "当前通过代理访问，点击改为直连"
    : "当前直连（适合云服务器），点击改为走代理";
}

function renderSaveMode() {
  document.querySelectorAll(".save-mode-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.saveMode === state.saveMode);
  });
  $("saveModeHint").textContent = state.saveMode === "server"
    ? "下载到服务器项目的 download/ 目录，完成后自动标记「已下载」"
    : "后端只返回下载地址，由浏览器循环下载到你自己的电脑";
}

function renderBatchInfo() {
  const box = $("batchInfo");
  if (!state.batchId) {
    box.textContent = "未选择批次：显示全部作品（共 " + state.batches.length + " 个批次）";
    return;
  }
  const batch = state.batches.find((b) => String(b.run_id) === String(state.batchId));
  if (!batch) { box.textContent = "批次 #" + state.batchId; return; }
  const when = fmtTime(batch.finished_at || batch.started_at);
  box.innerHTML = "批次 <b>#" + esc(batch.run_id) + "</b> 抓取自 <code>" + esc(batch.url || "-")
    + "</code> · " + esc(when) + " · 本批入库 " + esc(batch.works || 0) + " 部"
    + (batch.status && batch.status !== "done" ? " · 状态 " + esc(batch.status) : "");
}

function applyBatch(batchId) {
  state.batchId = batchId || "";
  state.page = 1;
  renderBatchInfo();
  if (state.tab === "library") loadWorks().catch((error) => toast(error.message, "error"));
}

/* ==================================================================
 * 作品库
 * ================================================================== */
async function loadWorks() {
  const data = await api("/api/works?" + buildParams().toString(), { loadingText: "加载作品列表…" });
  state.data = data;
  state.page = data.page;
  renderStats(data);
  renderLanguageBar(data);
  renderGrid(data);
  renderPager(data);
}

function renderStats(data) {
  const cards = [
    { label: "当前结果", value: data.total, hint: "筛选后" },
    { label: "去重前条目", value: data.raw_total, hint: "当前搜索条件" },
    { label: "去重后作品", value: data.unique_total, hint: state.dedupe ? "已合并" : "未启用去重" },
    { label: "语言种类", value: data.language_stats.length, hint: "库内语言数" },
  ];
  $("statsRow").innerHTML = cards.map((card) =>
    '<div class="stat"><div class="label">' + esc(card.label) + '</div>'
    + '<div class="value">' + esc(card.value) + '<small>' + esc(card.hint) + "</small></div></div>"
  ).join("");
}

function chipHtml(key, label, count) {
  const active = (key === "" && state.languages.length === 0) || state.languages.includes(key);
  return '<button class="chip' + (active ? " active" : "") + '" data-lang="' + esc(key) + '">'
    + esc(label) + '<span class="chip-count">' + count + "</span></button>";
}

function renderLanguageBar(data) {
  const chips = [chipHtml("", "全部", data.unique_total)];
  (data.language_stats || []).forEach((entry) => chips.push(chipHtml(entry.key, entry.label, entry.count)));
  $("langBar").innerHTML = chips.join("");
}

/* ---------- 卡片 ---------- */
function cardHtml(item) {
  const localCover = item.cover_url
    ? '<img class="thumb" src="' + esc(item.cover_url) + '" alt="" loading="lazy" '
      + 'onerror="this.onerror=null;this.src=\'' + esc(item.thumb || "") + '\';">'
    : (item.thumb
        ? '<img class="thumb" src="' + esc(item.thumb) + '" alt="" loading="lazy" referrerpolicy="no-referrer" '
          + 'onerror="this.classList.add(\'broken\');this.removeAttribute(\'src\');">'
        : '<div class="thumb placeholder">无封面</div>');

  const gid = String(item.gallery_id);
  // 左上角状态角标：已下载优先，其次待下载
  const cornerBadge = item.downloaded
    ? '<span class="corner-badge done">已下载</span>'
    : (item.want_download ? '<span class="corner-badge want">待下载</span>' : "");
  const wantBtn = item.want_download
    ? '<button class="want-btn active" data-want-ids="' + gid + '" title="取消待下载">★ 待下载</button>'
    : '<button class="want-btn" data-want-ids="' + gid + '" title="标记为待下载">☆ 待下载</button>';
  const dlBtn = '<button class="dl-btn" data-dl-id="' + gid + '" title="获取下载地址，用自己的网络下载">立即下载</button>';
  const hideIds = (item.ids || [item.gallery_id]).join(",");
  const hideBtn = '<button class="hide-btn" data-hide-ids="' + esc(hideIds) + '" title="逻辑删除：不展示、不下载；再次抓取到也会跳过">隐藏</button>';
  const doneBadge = item.downloaded ? '<span class="badge done">已下载</span>' : "";
  const uncensoredBadge = item.uncensored ? '<span class="badge uncensored">无码</span>' : "";
  const siteBadge = item.site_favorite ? '<span class="badge site">站点收藏</span>' : "";
  const variantBadge = item.variant_count > 1
    ? '<span class="badge variants">' + item.variant_count + " 个版本</span>" : "";

  const langBadges = (item.languages || []).map((lang) =>
    '<span class="badge lang-' + esc(lang.key) + '">' + esc(lang.label) + "</span>").join("");
  const tagList = item.tags || [];
  const tagBadges = tagList.slice(0, 8).map((tag) => '<span class="badge tag">' + esc(tag) + "</span>").join("");
  const more = tagList.length > 8 ? '<span class="badge more">+' + (tagList.length - 8) + "</span>" : "";

  const variants = item.variant_count > 1
    ? '<details class="variants-list"><summary>查看 ' + item.variant_count + ' 个版本</summary><ul>'
      + item.variants.map((variant) =>
        '<li title="' + esc(variant.title_pretty || variant.title) + '">'
        + (variant.want_download ? '<span class="badge fav-mark">★</span> ' : "")
        + '<span class="badge lang-' + esc(variant.language) + '">' + esc(variant.language_label) + "</span> "
        + '<a href="' + esc(variant.url || "#") + '" target="_blank" rel="noopener noreferrer">#' + esc(variant.gallery_id) + "</a></li>"
      ).join("") + "</ul></details>"
    : "";

  const originalLine = (item.title_original && item.title_original !== item.title)
    ? '<div class="card-original" title="' + esc(item.title_original) + '">' + esc(item.title_original) + "</div>"
    : "";

  const facts = [];
  if (item.num_pages) facts.push(item.num_pages + " 页");
  if (item.num_favorites) facts.push(item.num_favorites + " 收藏");
  if (item.upload_date) facts.push("上传 " + fmtDate(item.upload_date));
  const factLine = facts.length ? facts.join(" · ") : "收录 " + fmtDate(item.last_seen);

  return '<article class="card">'
    + '<div class="thumb-wrap"><a href="' + esc(item.url || "#") + '" target="_blank" rel="noopener noreferrer">'
    + localCover + "</a>" + cornerBadge + "</div>"
    + '<div class="card-body">'
    + '<h3 class="card-title"><a href="' + esc(item.url || "#") + '" target="_blank" rel="noopener noreferrer">'
    + esc(item.title) + "</a></h3>"
    + originalLine
    + '<div class="badges">' + uncensoredBadge + doneBadge + siteBadge + variantBadge + langBadges + "</div>"
    + '<div class="badges">' + tagBadges + more + "</div>"
    + '<div class="card-actions">' + wantBtn + dlBtn + hideBtn + "</div>"
    + variants
    + '<div class="card-meta"><span>#' + esc(item.gallery_id) + "</span><span>" + esc(factLine) + "</span></div>"
    + "</div></article>";
}

function renderGrid(data) {
  const grid = $("grid");
  if (!data.items.length) {
    const message = data.raw_total === 0 && !state.wantOnly && !state.downloadedOnly && !state.q
      ? "数据库里还没有数据：在上方输入列表页 URL，点击「开始抓取」。"
      : "没有符合当前筛选条件的作品。";
    grid.innerHTML = '<div class="empty">' + esc(message) + "</div>";
    return;
  }
  grid.innerHTML = data.items.map(cardHtml).join("");
}

function pageButton(pageNo, current) {
  return '<button class="page-btn' + (pageNo === current ? " current" : "") + '" data-page="' + pageNo + '">' + pageNo + "</button>";
}

function renderPager(data) {
  const pager = $("pager");
  if (state.tab === "favorites") {
    const total = data.total || 0;
    pager.innerHTML = '<button class="page-btn" data-fav-page="' + Math.max(1, (data.page || 1) - 1) + '"'
      + ((data.page || 1) <= 1 ? " disabled" : "") + ">上一页</button>"
      + '<span class="page-info">收藏第 ' + (data.page || 1) + " / " + (data.num_pages || 1) + " 页 · 共 " + total + " 项</span>"
      + '<button class="page-btn" data-fav-page="' + ((data.page || 1) + 1) + '"'
      + ((data.page || 1) >= (data.num_pages || 1) ? " disabled" : "") + ">下一页</button>";
    return;
  }
  if (!data.total) { pager.innerHTML = ""; return; }
  const parts = [];
  parts.push('<button class="page-btn" data-page="' + (data.page - 1) + '"' + (data.page <= 1 ? " disabled" : "") + ">上一页</button>");
  const start = Math.max(1, data.page - 2);
  const end = Math.min(data.pages, data.page + 2);
  if (start > 1) parts.push(pageButton(1, data.page));
  if (start > 2) parts.push('<span class="dots">…</span>');
  for (let pageNo = start; pageNo <= end; pageNo += 1) parts.push(pageButton(pageNo, data.page));
  if (end < data.pages - 1) parts.push('<span class="dots">…</span>');
  if (end < data.pages) parts.push(pageButton(data.pages, data.page));
  parts.push('<button class="page-btn" data-page="' + (data.page + 1) + '"' + (data.page >= data.pages ? " disabled" : "") + ">下一页</button>");
  parts.push('<span class="page-info">第 ' + data.page + " / " + data.pages + " 页 · 共 " + data.total + " 个作品</span>");
  pager.innerHTML = parts.join("");
}

/* ==================================================================
 * 我的收藏（站点）
 * ================================================================== */
async function loadFavorites(page = 1) {
  if (!state.user && !state.key) { showGate(); return; }
  const params = new URLSearchParams({ page: String(page) });
  if (state.favQuery) params.set("q", state.favQuery);
  const data = await api("/api/site-favorites?" + params.toString());
  state.favData = data;
  state.favPage = data.page || page;
  $("favSummary").textContent = "共 " + (data.total || 0) + " 项收藏 · 第 " + (data.page || 1) + " / " + (data.num_pages || 1) + " 页";
  renderGrid(data);
  renderPager(data);
  $("statsRow").innerHTML = "";
  $("langBar").innerHTML = "";
}

/* ==================================================================
 * 标记与下载
 * ================================================================== */
async function toggleWant(ids, want) {
  await api("/api/want-download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: ids, want: want }),
  });
}

async function markDownloaded(ids, flag) {
  await api("/api/downloaded", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: ids, downloaded: flag }),
  });
}

async function setHidden(ids, flag) {
  await api("/api/hidden", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: ids, hidden: flag }),
  });
}

async function syncFavoritesToWant() {
  if (!state.user && !state.key) { showGate(); return; }
  if (!window.confirm("将站点账号收藏列表中的全部作品，在本地数据库标记为「待下载」？")) return;
  $("favSyncBtn").disabled = true;
  try {
    const data = await api("/api/favorites/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    toast("同步完成：共 " + (data.synced || 0) + " 条标记为待下载", "ok");
    await reloadAll();
  } catch (error) {
    toast("同步失败：" + error.message, "error");
  } finally {
    $("favSyncBtn").disabled = false;
  }
}

/** 构造本服务的文件转发地址（同源 + attachment，浏览器会直接保存）。 */
/* 说明：立即下载已改为「先入队，下载完成后自动保存」，此函数用于完成后的取文件。 */
function redownloadEnabled() {
  const box = $("redownloadCheck");
  return Boolean(box && box.checked);
}

function downloadFileUrl(galleryId, format) {
  const params = new URLSearchParams({
    gallery_id: String(galleryId),
    format: format || "zip",
    apikey: state.key || "",
  });
  if (redownloadEnabled()) params.set("redownload", "1");
  return "/api/download/file?" + params.toString();
}

/** 队列轮询：所有页面的进度都来自同一份服务端队列。 */
function fmtBytes(value) {
  const n = Number(value) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

function jobRowHtml(job) {
  const status = job.status || "queued";
  const total = Number(job.total_bytes) || 0;
  const done = Number(job.done_bytes) || 0;
  const pct = status === "done" ? 100
    : (total > 0 ? Math.min(99, Math.round((done / total) * 100)) : 0);
  const statusText = { queued: "排队中", running: "下载中", done: "完成", failed: "失败", cancelled: "已取消" }[status] || status;
  const sizeText = total > 0 ? (fmtBytes(done) + " / " + fmtBytes(total)) : fmtBytes(done);
  const cancelBtn = (status === "queued" || status === "running")
    ? '<button class="btn ghost q-cancel" data-cancel-job="' + job.job_id + '">取消</button>' : "";
  const title = esc(job.title || ("#" + job.gallery_id));
  const error = job.error ? ' · <span style="color:#d93025">' + esc(job.error.slice(0, 60)) + "</span>" : "";
  return '<div class="queue-item ' + esc(status) + '">'
    + '<div class="q-name"><span class="q-title">' + title + "</span>"
    + '<span class="q-sub">#' + esc(job.gallery_id) + " · " + esc(statusText) + " · " + esc(sizeText) + error + "</span></div>"
    + '<div class="q-bar"><span style="width:' + pct + '%"></span></div>'
    + '<div class="q-pct">' + pct + "%</div>" + cancelBtn + "</div>";
}

function renderQueue(queue) {
  if (!queue) return;
  $("dlProgressPanel").hidden = false;
  const counts = queue.counts || {};
  $("dlProgressText").textContent = "并发 " + (queue.workers || 0)
    + " · 排队 " + (counts.queued || 0)
    + " · 下载中 " + (counts.running || 0)
    + " · 完成 " + (counts.done || 0)
    + (counts.failed ? " · 失败 " + counts.failed : "");
  const jobs = (queue.jobs || []).filter((job) =>
    ["running", "queued", "failed"].includes(job.status)).slice(0, 40);
  const finished = (queue.jobs || []).filter((job) => job.status === "done").slice(0, 3);
  const rows = jobs.concat(finished);
  $("queueList").innerHTML = rows.length
    ? rows.map(jobRowHtml).join("")
    : '<div class="queue-empty">队列为空</div>';

  // 立即下载：任务完成后自动把文件存到用户电脑
  for (const job of queue.jobs || []) {
    const pending = state.pendingDeliveries.get(String(job.job_id));
    if (job.status === "done" && pending) {
      state.pendingDeliveries.delete(String(job.job_id));
      queueDelivery(pending);
    } else if (["failed", "cancelled"].includes(job.status) && pending) {
      state.pendingDeliveries.delete(String(job.job_id));
      toast("下载失败：" + esc(job.error || "任务未完成"), "error");
    }
  }
}

function triggerBrowserDownload(galleryId) {
  const link = document.createElement("a");
  link.href = "/api/download/file?gallery_id=" + encodeURIComponent(galleryId)
    + "&format=" + encodeURIComponent($("formatSelect").value || "zip")
    + "&apikey=" + encodeURIComponent(state.key || "");
  document.body.appendChild(link);
  link.click();
  link.remove();
  toast("已从服务器保存到你的电脑");
}

function startDownloadPolling() {
  stopDownloadPolling();
  $("dlProgressPanel").hidden = false;
  state.dlPolling = window.setInterval(pollDownload, 1000);
  pollDownload();
}

function stopDownloadPolling() {
  if (state.dlPolling) { window.clearInterval(state.dlPolling); state.dlPolling = null; }
}

async function pollDownload() {
  let queue;
  try {
    queue = await api("/api/download/jobs?limit=60", { silent: true });
  } catch (error) {
    stopDownloadPolling();
    return;
  }
  renderQueue(queue);
  const counts = queue.counts || {};
  const active = (counts.queued || 0) + (counts.running || 0);
  $("downloadAllBtn").disabled = false;
  $("saveBatchBtn").disabled = false;
  $("queueCancelAllBtn").disabled = active === 0;
  if (state.queueWasActive && active === 0) {
    stopDownloadPolling();
    toast("下载队列已清空：完成 " + (counts.done || 0) + "，失败 " + (counts.failed || 0),
          counts.failed ? "error" : "ok");
    await reloadAll();
  }
  state.queueWasActive = active > 0;
}

/** 立即下载：加入服务端队列；完成后自动从服务器保存到用户电脑。 */
async function downloadNow(galleryId, button) {
  const fmt = $("formatSelect").value || "zip";
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "入队中…";
  try {
    const data = await api("/api/download/enqueue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gallery_id: galleryId, format: fmt }),
    });
    const jobId = data.job && data.job.job_id;
    if (jobId) state.pendingDeliveries.set(String(jobId), galleryId);
    toast("已加入下载队列，完成后会自动保存到你的电脑");
    startDownloadPolling();
  } catch (error) {
    toast("加入队列失败：" + error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/** 逐个投递（间隔 800ms），避免浏览器同时弹多个保存框。 */
function queueDelivery(galleryId) {
  state.deliveryQueue = state.deliveryQueue || [];
  state.deliveryQueue.push(galleryId);
  if (state.deliveryTimer) return;
  const pump = () => {
    const next = (state.deliveryQueue || []).shift();
    if (!next) { state.deliveryTimer = null; return; }
    triggerBrowserDownload(next);
    state.deliveryTimer = window.setTimeout(pump, 800);
  };
  state.deliveryTimer = window.setTimeout(pump, 100);
}

/** 服务端保存：把「待下载」或整批任务全部入队（队列负责并发与进度）。 */
async function startDownloadAll() {
  if (state.saveMode === "local") { return localLoop("want", 0); }
  const fmt = $("formatSelect").value || "zip";
  $("downloadAllBtn").disabled = true;
  $("dlProgressPanel").hidden = false;
  try {
    const data = await api("/api/download/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format: fmt, redownload: redownloadEnabled() }),
    });
    toast("已加入队列 " + (data.queued || 0) + " 个任务");
    startDownloadPolling();
  } catch (error) {
    $("downloadAllBtn").disabled = false;
    toast(error.message, "error");
  }
}

/** 保存全部：把当前批次的全部作品入队。 */
async function saveCurrentBatch() {
  if (!state.batchId) { toast("请先在顶部选择一个批次", "error"); return; }
  if (state.saveMode === "local") { return localLoop("batch", Number(state.batchId)); }
  const fmt = $("formatSelect").value || "zip";
  $("saveBatchBtn").disabled = true;
  $("dlProgressPanel").hidden = false;
  try {
    const data = await api("/api/batch/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        batch_id: Number(state.batchId), format: fmt, redownload: redownloadEnabled(),
      }),
    });
    toast("已加入队列 " + (data.queued || 0) + " 个任务");
    startDownloadPolling();
  } catch (error) {
    $("saveBatchBtn").disabled = false;
    toast(error.message, "error");
  }
}

/** 本地保存：目标先入队；队列下载完成后逐个自动保存到用户电脑。 */
async function localLoop(mode, batchId) {
  if (state.localRunning) { toast("已有批量任务在进行", "error"); return; }
  state.localRunning = true;
  state.localCancel = false;
  $("dlProgressPanel").hidden = false;
  const fmt = $("formatSelect").value || "zip";
  try {
    const params = new URLSearchParams({ mode: mode });
    if (mode === "batch") params.set("batch_id", String(batchId));
    if (redownloadEnabled()) params.set("redownload", "1");
    const plan = await api("/api/download/targets?" + params.toString(),
                           { loadingText: "准备下载清单…" });
    const items = plan.items || [];
    if (!items.length) {
      throw new Error(mode === "batch" ? "该批次没有可下载的作品" : "没有标记为「待下载」的作品");
    }
    let queued = 0;
    for (const target of items) {
      if (state.localCancel) break;
      const data = await api("/api/download/enqueue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ gallery_id: target.gallery_id, format: fmt }),
      });
      const jobId = data.job && data.job.job_id;
      if (jobId) {
        state.pendingDeliveries.set(String(jobId), target.gallery_id);
        queued += 1;
      }
    }
    toast("已加入队列 " + queued + " 个任务，完成后会依次保存到你的电脑");
    startDownloadPolling();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.localRunning = false;
  }
}

/* ==================================================================
 * 抓取
 * ================================================================== */
async function startScrape() {
  const url = $("urlInput").value.trim();
  if (!url) { toast("请先填写列表页 URL", "error"); return; }
  state.finishedRunId = null;
  $("startBtn").disabled = true;
  $("progressPanel").hidden = false;
  try {
    await api("/api/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: url,
        max_pages: Number($("maxPages").value || 0),
        delay: Number($("delay").value || 1.2),
        enrich: $("enrichCheck").checked,
        covers: $("coverCheck").checked,
        incremental: true,
      }),
    });
    toast("已开始抓取");
    startPolling();
  } catch (error) {
    $("startBtn").disabled = false;
    toast(error.message, "error");
  }
}

function startPolling() { stopPolling(); state.scrapePolling = window.setInterval(pollStatus, 1000); pollStatus(); }
function stopPolling() { if (state.scrapePolling) { window.clearInterval(state.scrapePolling); state.scrapePolling = null; } }

async function pollStatus() {
  let status;
  try { status = await api("/api/scrape/status", { silent: true }); }
  catch (error) { stopPolling(); $("startBtn").disabled = false; return; }
  renderProgress(status);
  $("startBtn").disabled = Boolean(status.running);
  $("cancelBtn").disabled = !status.running;
  if (!status.running && ["done", "cancelled", "error"].includes(status.status) && state.finishedRunId !== status.run_id) {
    state.finishedRunId = status.run_id;
    if (status.status === "done") {
      toast("抓取完成：条目 " + status.items_seen + "（新增 " + status.items_new + "）", "ok");
      reloadAll();
    } else if (status.status === "cancelled") {
      toast("抓取已取消"); reloadAll();
    } else {
      toast(status.error || "抓取失败", "error");
      loadMeta().catch(() => {});
    }
    stopPolling();
  }
}

function renderProgress(status) {
  $("progressPanel").hidden = false;
  const done = status.pages_done || 0;
  const total = status.pages_total || 0;
  const running = Boolean(status.running);
  const bar = $("progressBar");
  if (total > 0) { bar.classList.remove("indeterminate"); bar.style.width = Math.min(100, Math.round((done / total) * 100)) + "%"; }
  else if (running) { bar.classList.add("indeterminate"); bar.style.width = "35%"; }
  else { bar.classList.remove("indeterminate"); bar.style.width = done > 0 ? "100%" : "0%"; }
  const map = { idle: "空闲", running: "抓取中", done: "已完成", cancelled: "已取消", error: "失败" };
  const detail = (status.details_total || status.details_done)
    ? " · 详情 " + (status.details_done || 0) + "/" + (status.details_total || 0) : "";
  const coverText = (status.covers_done || status.covers_failed)
    ? " · 封面 " + (status.covers_done || 0) + " 张" : "";
  $("progressText").innerHTML = "状态：<b>" + esc(map[status.status] || status.status) + "</b> · 页 " + done + "/" + (total || "?")
    + " · 发现 " + (status.items_seen || 0) + " · 新增 " + (status.items_new || 0) + " · 重复 " + (status.items_dup || 0)
    + detail + coverText + (status.error ? " · " + esc(status.error) : "");
  $("logBox").textContent = (status.logs || []).slice(-12).join("\n");
}

/* ==================================================================
 * Tab 切换与刷新
 * ================================================================== */
async function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tab));
  $("libraryPanel").hidden = tab !== "library";
  $("favoritesPanel").hidden = tab !== "favorites";
  document.querySelectorAll("#libraryPanel ~ .panel, .panel").forEach(() => {});
  if (tab === "favorites") {
    $("statsRow").innerHTML = "";
    $("langBar").innerHTML = "";
    try { await loadFavorites(1); } catch (error) { toast(error.message, "error"); }
  } else {
    renderBatchInfo();
    try { await loadWorks(); } catch (error) { toast(error.message, "error"); }
  }
}

async function reloadAll() {
  await Promise.all([loadMeta().catch(() => {}), state.tab === "favorites" ? loadFavorites(state.favPage).catch(() => {}) : loadWorks().catch(() => {})]);
}

/* ==================================================================
 * 事件绑定
 * ================================================================== */
let searchTimer = null;
let favSearchTimer = null;

function bindEvents() {
  $("gateBtn").addEventListener("click", submitGate);
  $("gateInput").addEventListener("keydown", (event) => { if (event.key === "Enter") submitGate(); });
  const clearKey = (message) => {
    deleteCookie(KEY_COOKIE);
    state.key = "";
    toast(message);
    showGate("已清除保存的 Key，请重新输入。");
  };
  $("gateClear").addEventListener("click", () => clearKey("已清除浏览器里保存的 Key"));
  $("changeKeyBtn").addEventListener("click", () => {
    // 登录用户的 Key 跟账号走，去设置面板改；匿名用户才走清 cookie 门禁
    if (state.user) { openSettingsModal(); return; }
    clearKey("已清除保存的 Key，请重新输入");
  });

  // 认证 / 设置 / 同步 弹窗
  document.querySelectorAll(".modal-tab").forEach((btn) => {
    btn.addEventListener("click", () => openAuthModal(btn.dataset.authTab));
  });
  $("authClose").addEventListener("click", closeAuthModal);
  $("authSubmit").addEventListener("click", submitAuth);
  $("authPassword").addEventListener("keydown", (event) => { if (event.key === "Enter") submitAuth(); });
  $("settingsClose").addEventListener("click", closeSettingsModal);
  $("settingsSave").addEventListener("click", saveSettings);
  $("syncBtn").addEventListener("click", doSync);
  // 点弹窗遮罩关闭
  $("authModal").addEventListener("click", (event) => { if (event.target.id === "authModal") closeAuthModal(); });
  $("settingsModal").addEventListener("click", (event) => { if (event.target.id === "settingsModal") closeSettingsModal(); });

  $("proxyBtn").addEventListener("click", async () => {
    const next = !state.proxyEnabled;
    $("proxyBtn").disabled = true;
    try {
      const data = await api("/api/settings/proxy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      state.proxyEnabled = Boolean(data.proxy_enabled);
      renderProxy({ proxy_enabled: data.proxy_enabled, proxy_address: data.proxy_address });
      toast(state.proxyEnabled ? "已开启代理" : "已关闭代理，改为直连", "ok");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      $("proxyBtn").disabled = false;
    }
  });

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  $("startBtn").addEventListener("click", startScrape);
  $("urlInput").addEventListener("keydown", (event) => { if (event.key === "Enter") startScrape(); });
  $("cancelBtn").addEventListener("click", async () => {
    try { await api("/api/scrape/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); toast("已发送取消请求"); }
    catch (error) { toast(error.message, "error"); }
  });

  $("batchSelect").addEventListener("change", (event) => applyBatch(event.target.value));
  $("batchSearch").addEventListener("input", (event) => {
    state.batchSearch = event.target.value;
    renderBatchOptions();
  });

  document.querySelectorAll(".save-mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.localRunning) { toast("本地下载进行中，无法切换", "error"); return; }
      state.saveMode = btn.dataset.saveMode;
      renderSaveMode();
    });
  });
  $("saveBatchBtn").addEventListener("click", saveCurrentBatch);
  $("queueCancelAllBtn").addEventListener("click", async () => {
    try {
      await api("/api/download/jobs/cancel", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ all: true }),
      });
      toast("已取消排队中的任务");
      pollDownload();
    } catch (error) { toast(error.message, "error"); }
  });

  $("queueList").addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-cancel-job]");
    if (!btn) return;
    try {
      await api("/api/download/jobs/cancel", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: Number(btn.dataset.cancelJob) }),
      });
      pollDownload();
    } catch (error) { toast(error.message, "error"); }
  });

  $("downloadAllBtn").addEventListener("click", startDownloadAll);
  $("downloadCancelBtn").addEventListener("click", async () => {
    if (state.localRunning) {
      state.localCancel = true;
      toast("已请求停止本地下载");
      return;
    }
    try { await api("/api/download/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); toast("已发送停止请求"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("favReloadBtn").addEventListener("click", () => loadFavorites(1).catch((e) => toast(e.message, "error")));
  $("favSyncBtn").addEventListener("click", syncFavoritesToWant);
  $("favSearchInput").addEventListener("input", (event) => {
    const value = event.target.value;
    if (favSearchTimer) window.clearTimeout(favSearchTimer);
    favSearchTimer = window.setTimeout(() => {
      state.favQuery = value.trim();
      loadFavorites(1).catch((error) => toast(error.message, "error"));
    }, 350);
  });

  $("searchInput").addEventListener("input", (event) => {
    const value = event.target.value;
    if (searchTimer) window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.q = value.trim();
      state.page = 1;
      if (state.tab === "library") loadWorks().catch((error) => toast(error.message, "error"));
    }, 320);
  });

  const reload = () => { state.page = 1; if (state.tab === "library") loadWorks().catch((e) => toast(e.message, "error")); };
  $("dedupeCheck").addEventListener("change", (e) => { state.dedupe = e.target.checked; reload(); });
  $("wantCheck").addEventListener("change", (e) => { state.wantOnly = e.target.checked; reload(); });
  $("downloadedCheck").addEventListener("change", (e) => { state.downloadedOnly = e.target.checked; reload(); });
  $("uncensoredCheck").addEventListener("change", (e) => { state.uncensored = e.target.checked; reload(); });
  $("sortSelect").addEventListener("change", (e) => { state.sort = e.target.value; reload(); });
  $("perPageSelect").addEventListener("change", (e) => { state.perPage = Number(e.target.value) || 48; reload(); });

  $("exportBtn").addEventListener("click", () => {
    const params = buildParams(false);
    if (state.key) params.set("apikey", state.key);
    window.location.href = "/api/export.csv?" + params.toString();
  });

  $("resetBtn").addEventListener("click", async () => {
    if (!window.confirm("确定清空本地 SQLite 中的所有作品数据与抓取记录吗？此操作不可撤销。")) return;
    try {
      await api("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      toast("已清空本地数据", "ok");
      state.page = 1;
      await reloadAll();
    } catch (error) { toast(error.message, "error"); }
  });

  // 卡片按钮：待下载 / 立即下载（事件委托）
  $("grid").addEventListener("click", async (event) => {
    const wantBtn = event.target.closest("button.want-btn");
    if (wantBtn) {
      const id = Number(wantBtn.dataset.wantIds);
      const was = wantBtn.classList.contains("active");
      wantBtn.disabled = true;
      try {
        await toggleWant([id], !was);
        toast(was ? "已取消待下载" : "已标记待下载 ★", "ok");
        await reloadAll();
      } catch (error) { toast(error.message, "error"); wantBtn.disabled = false; }
      return;
    }
    const dlBtn = event.target.closest("button.dl-btn");
    if (dlBtn) {
      await downloadNow(Number(dlBtn.dataset.dlId), dlBtn);
      return;
    }
    const markBtn = event.target.closest("button.mark-done-btn");
    if (markBtn) {
      const id = Number(markBtn.dataset.doneIds);
      const was = markBtn.dataset.done === "1";
      try { await markDownloaded([id], !was); toast(was ? "已取消已下载标记" : "已标记已下载", "ok"); await reloadAll(); }
      catch (error) { toast(error.message, "error"); }
    }
    const hideBtn = event.target.closest("button.hide-btn");
    if (hideBtn) {
      const ids = String(hideBtn.dataset.hideIds || "").split(",")
        .map((value) => Number(value)).filter((value) => Number.isFinite(value) && value > 0);
      if (!ids.length) return;
      if (!window.confirm("确定隐藏该作品吗？隐藏后不再展示、不参与下载；再次抓取到也会跳过。")) return;
      hideBtn.disabled = true;
      try {
        await setHidden(ids, true);
        toast("已隐藏（逻辑删除）", "ok");
        await reloadAll();
      } catch (error) { toast(error.message, "error"); hideBtn.disabled = false; }
      return;
    }
  });

  $("langBar").addEventListener("click", (event) => {
    const button = event.target.closest("button.chip");
    if (!button) return;
    const key = button.dataset.lang || "";
    if (!key) state.languages = [];
    else if (state.languages.includes(key)) state.languages = state.languages.filter((item) => item !== key);
    else state.languages = state.languages.concat(key);
    state.page = 1;
    if (state.tab === "library") loadWorks().catch((error) => toast(error.message, "error"));
  });

  $("pager").addEventListener("click", (event) => {
    const favBtn = event.target.closest("button[data-fav-page]");
    if (favBtn && !favBtn.disabled) {
      loadFavorites(Number(favBtn.dataset.favPage)).catch((error) => toast(error.message, "error"));
      return;
    }
    const button = event.target.closest("button.page-btn");
    if (!button || button.disabled) return;
    const pageNo = Number(button.dataset.page);
    if (!pageNo || pageNo < 1) return;
    state.page = pageNo;
    loadWorks().catch((error) => toast(error.message, "error"));
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

/* ==================================================================
 * 启动
 * ================================================================== */
(async function init() {
  bindEvents();
  renderSaveMode();
  // 先确认登录态：登录用户的站点域名 / Key 跟账号走，不需要门禁
  await loadAuth();
  // 匿名模式兼容：URL ?apikey= / cookie 里的 Key 仍然可用
  initApiKey();
  await loadMeta().catch(() => {});
  if (state.user) {
    hideGate();
    try { await loadWorks(); } catch (error) { toast("加载失败：" + error.message, "error"); }
    return;
  }
  if (!state.key) {
    prefillGate(state.meta);
    showGate();
    return;
  }
  try {
    await loadWorks();
  } catch (error) {
    toast("加载失败：" + error.message, "error");
  }
})();