"use strict";

/* ==================================================================
 * 状态
 * ================================================================== */
const state = {
  key: "",
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
  if (fromUrl) {
    writeCookie(KEY_COOKIE, fromUrl);
    params.delete("apikey");
    const query = params.toString();
    const clean = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
    window.history.replaceState(null, "", clean);   // 避免 Key 留在历史记录里
  }
  state.key = fromUrl || readCookie(KEY_COOKIE);
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

async function submitGate() {
  const value = ($("gateInput").value || "").trim();
  if (!value) { toast("请输入 API Key", "error"); return; }
  $("gateBtn").disabled = true;
  try {
    const response = await fetch("/api/site-favorites?page=1", { headers: { "X-API-Key": value } });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
    state.key = value;
    writeCookie(KEY_COOKIE, value);
    hideGate();
    toast("API Key 校验通过", "ok");
    await reloadAll();
  } catch (error) {
    toast("Key 无效：" + error.message, "error");
    $("gateHint").textContent = "校验失败：" + error.message;
  } finally {
    $("gateBtn").disabled = false;
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

async function api(path, options = {}) {
  const opts = Object.assign({}, options);
  opts.headers = Object.assign({}, options.headers || {});
  if (state.key) opts.headers["X-API-Key"] = state.key;
  const response = await fetch(path, opts);
  let data = {};
  try { data = await response.json(); } catch (error) { /* 非 JSON */ }
  if (response.status === 401) {
    showGate("需要有效的 API Key 才能继续。");
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
  $("proxyChip").textContent = meta.proxy ? "代理 " + meta.proxy : "未启用代理";
  const parts = ["库内 " + meta.stats.entries + " 条", "去重后 " + meta.stats.works + " 部"];
  if (meta.stats.want_download) parts.push("待下载 " + meta.stats.want_download);
  if (meta.stats.downloaded) parts.push("已下载 " + meta.stats.downloaded);
  $("dbChip").textContent = parts.join(" / ");
  $("dbChip").title = meta.db_path;
  if (meta.download_dir) $("dlDirHint").textContent = meta.download_dir;
  renderLastRun(meta.stats.last_run);
  renderDownloadSummary(meta.download_status);
  state.batches = meta.batches || [];
  renderBatchOptions();
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
  const data = await api("/api/works?" + buildParams().toString());
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
    + '<div class="card-actions">' + wantBtn + dlBtn + "</div>"
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
  if (!state.key) { showGate(); return; }
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

/** 立即下载：后端只返回下载地址，交给浏览器用用户自己的网络下载。 */
async function downloadNow(galleryId, button) {
  const fmt = $("formatSelect").value || "zip";
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "获取中…";
  try {
    const data = await api("/api/download-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gallery_id: galleryId, format: fmt }),
    });
    if (!data.url) throw new Error("未取到下载地址");
    const link = document.createElement("a");
    link.href = data.url;
    link.download = data.filename || "";
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    toast("已获取下载地址，开始用你的网络下载" + (data.expires_at ? "（链接短时效，请尽快完成）" : ""));
  } catch (error) {
    toast("取下载地址失败：" + error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/** 保存全部：把当前批次的全部作品保存到 download/batch_<批次号>/ */
async function saveCurrentBatch() {
  if (!state.batchId) {
    toast("请先在顶部选择一个批次", "error");
    return;
  }
  const fmt = $("formatSelect").value || "zip";
  $("saveBatchBtn").disabled = true;
  $("dlProgressPanel").hidden = false;
  try {
    await api("/api/batch/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_id: Number(state.batchId), format: fmt }),
    });
    toast("已开始保存批次 #" + state.batchId + " 的全部数据");
    startDownloadPolling();
  } catch (error) {
    $("saveBatchBtn").disabled = false;
    toast(error.message, "error");
  }
}

async function startDownloadAll() {
  const fmt = $("formatSelect").value || "zip";
  $("downloadAllBtn").disabled = true;
  $("dlProgressPanel").hidden = false;
  try {
    await api("/api/download/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format: fmt }),
    });
    toast("已开始批量下载");
    startDownloadPolling();
  } catch (error) {
    $("downloadAllBtn").disabled = false;
    toast(error.message, "error");
  }
}

function startDownloadPolling() {
  stopDownloadPolling();
  state.dlPolling = window.setInterval(pollDownload, 1000);
  pollDownload();
}

function stopDownloadPolling() {
  if (state.dlPolling) { window.clearInterval(state.dlPolling); state.dlPolling = null; }
}

async function pollDownload() {
  let status;
  try {
    status = await api("/api/download/status");
  } catch (error) {
    stopDownloadPolling();
    $("downloadAllBtn").disabled = false;
    return;
  }
  const total = status.total || 0;
  const done = status.done || 0;
  const failed = status.failed || 0;
  const bar = $("dlProgressBar");
  bar.classList.toggle("indeterminate", Boolean(status.running) && !total);
  bar.style.width = total ? Math.min(100, Math.round(((done + failed) / total) * 100)) + "%" : (status.running ? "35%" : "0%");
  const map = { idle: "空闲", running: "下载中", done: "已完成", cancelled: "已取消", error: "失败" };
  $("dlProgressText").innerHTML = "状态：<b>" + esc(map[status.status] || status.status) + "</b>"
    + (status.out_dir ? " · 输出 <code>" + esc(status.out_dir) + "</code>" : "") + " · 成功 "
    + done + "/" + total + (failed ? " · 失败 " + failed : "")
    + (status.current ? " · 当前 " + esc(status.current) : "")
    + (status.error ? " · " + esc(status.error) : "");
  $("dlLogBox").textContent = (status.logs || []).slice(-12).join("\n");
  renderDownloadSummary(status);
  $("downloadAllBtn").disabled = Boolean(status.running);
  $("saveBatchBtn").disabled = Boolean(status.running);
  $("downloadCancelBtn").disabled = !status.running;
  if (!status.running && status.status !== "running" && status.status !== "idle") {
    stopDownloadPolling();
    toast("批量下载结束：成功 " + done + "，失败 " + failed, failed ? "error" : "ok");
    await reloadAll();
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
  try { status = await api("/api/scrape/status"); }
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
  $("gateClear").addEventListener("click", () => {
    deleteCookie(KEY_COOKIE);
    state.key = "";
    toast("已清除浏览器里保存的 Key");
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

  $("saveBatchBtn").addEventListener("click", saveCurrentBatch);
  $("downloadAllBtn").addEventListener("click", startDownloadAll);
  $("downloadCancelBtn").addEventListener("click", async () => {
    try { await api("/api/download/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); toast("已发送停止请求"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("favReloadBtn").addEventListener("click", () => loadFavorites(1).catch((e) => toast(e.message, "error")));
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
  initApiKey();
  if (!state.key) {
    showGate();
    return;
  }
  try {
    await loadMeta();
    await loadWorks();
  } catch (error) {
    if (!state.key) return;
    toast("加载失败：" + error.message, "error");
  }
})();