"use strict";

const state = {
  q: "",
  languages: [],
  dedupe: true,
  uncensored: false,
  sort: "priority",
  page: 1,
  perPage: 48,
  data: null,
  meta: null,
  polling: null,
  finishedRunId: null,
  urlInitialized: false,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function fmtDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return (iso || "").slice(0, 10);
  return date.toLocaleDateString("zh-CN");
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch (error) { /* 忽略非 JSON 响应 */ }
  if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
  return data;
}

function buildParams(includePaging = true) {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  state.languages.forEach((lang) => params.append("lang", lang));
  params.set("dedupe", state.dedupe ? "1" : "0");
  if (state.uncensored) params.set("uncensored", "1");
  params.set("sort", state.sort);
  if (includePaging) {
    params.set("page", String(state.page));
    params.set("per_page", String(state.perPage));
  }
  return params;
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

/* ---------------- 元数据 ---------------- */
async function loadMeta() {
  const meta = await api("/api/meta");
  state.meta = meta;
  if (!state.urlInitialized) {
    $("urlInput").value = meta.default_url || "";
    state.urlInitialized = true;
  }
  $("proxyChip").textContent = meta.proxy ? "代理 " + meta.proxy : "未启用代理";
  $("proxyHint").textContent = meta.proxy || "未启用代理";
  if (meta.cover_dir) $("coverHint").textContent = meta.cover_dir;
  const statParts = ["库内 " + meta.stats.entries + " 条", "去重后 " + meta.stats.works + " 部"];
  if (meta.stats.details) statParts.push("详情 " + meta.stats.details);
  if (meta.stats.covers) statParts.push("封面 " + meta.stats.covers);
  $("dbChip").textContent = statParts.join(" / ");
  $("dbChip").title = meta.db_path;
  renderLastRun(meta.stats.last_run);
  if (meta.scrape && meta.scrape.running) {
    startPolling();
  } else if (meta.scrape && ["done", "cancelled", "error"].includes(meta.scrape.status)) {
    renderProgress(meta.scrape);
  }
}

function renderLastRun(run) {
  const chip = $("lastRunChip");
  if (!run) {
    chip.textContent = "暂无抓取记录";
    chip.title = "";
    return;
  }
  const statusText = { done: "成功", error: "失败", cancelled: "已取消", running: "进行中" }[run.status] || run.status;
  chip.textContent = "上次抓取 " + fmtTime(run.finished_at || run.started_at) + " · " + statusText + " · 新增 " + (run.items_new || 0);
  chip.title = run.url || "";
}

/* ---------------- 列表数据 ---------------- */
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
    { label: "当前筛选结果", value: data.total, hint: "去重 + 语言过滤后" },
    { label: "去重前条目", value: data.raw_total, hint: "当前搜索条件下" },
    { label: "去重后作品", value: data.unique_total, hint: state.dedupe ? "同作品已合并" : "未启用去重" },
    { label: "语言种类", value: data.language_stats.length, hint: "当前库内语言标签数" },
  ];
  $("statsRow").innerHTML = cards.map((card) => (
    '<div class="stat"><div class="label">' + esc(card.label) + '</div>' +
    '<div class="value">' + esc(card.value) + '<small>' + esc(card.hint) + '</small></div></div>'
  )).join("");
}

function chipHtml(key, label, count) {
  const active = (key === "" && state.languages.length === 0) || state.languages.includes(key);
  return '<button class="chip' + (active ? ' active' : '') + '" data-lang="' + esc(key) + '">' +
    esc(label) + '<span class="chip-count">' + count + "</span></button>";
}

function renderLanguageBar(data) {
  const chips = [chipHtml("", "全部", data.unique_total)];
  (data.language_stats || []).forEach((entry) => chips.push(chipHtml(entry.key, entry.label, entry.count)));
  $("langBar").innerHTML = chips.join("");
}

function renderGrid(data) {
  const grid = $("grid");
  if (!data.items.length) {
    const message = data.raw_total === 0
      ? "数据库里还没有数据：在上方输入列表页 URL，点击「开始抓取」。"
      : "没有符合当前筛选条件的作品。";
    grid.innerHTML = '<div class="empty">' + esc(message) + "</div>";
    return;
  }
  grid.innerHTML = data.items.map(cardHtml).join("");
}

function cardHtml(item) {
  const remoteThumb = item.thumb
    ? '<div class="thumb placeholder">加载中…</div>'
    : '<div class="thumb placeholder">无封面</div>';
  // 封面已下载到本地时优先用本地文件，失败再回落到远端代理
  const localCover = item.cover_url
    ? '<img class="thumb" src="' + esc(item.cover_url) + '" alt="" loading="lazy" ' +
      'onerror="this.onerror=null;this.src=\'' + esc(item.thumb || "") + '\';">'
    : (item.thumb
        ? '<img class="thumb" src="' + esc(item.thumb) + '" alt="" loading="lazy" referrerpolicy="no-referrer" ' +
          'onerror="this.classList.add(\'broken\');this.removeAttribute(\'src\');">'
        : remoteThumb);

  const langBadges = (item.languages || []).map((lang) =>
    '<span class="badge lang-' + esc(lang.key) + '">' + esc(lang.label) + "</span>").join("");
  const tagList = item.tags || [];
  const tagBadges = tagList.slice(0, 8).map((tag) => '<span class="badge tag">' + esc(tag) + "</span>").join("");
  const more = tagList.length > 8 ? '<span class="badge more">+' + (tagList.length - 8) + "</span>" : "";
  // 收藏只作用于「按优先级排序第一」的推荐版本，不标记其它语言版本
  const favIds = String(item.gallery_id);
  const favBtn = item.favorite
    ? '<button class="fav-btn active" data-fav-ids="' + favIds + '" title="取消收藏（仅推荐版本）">\u2665</button>'
    : '<button class="fav-btn" data-fav-ids="' + favIds + '" title="收藏推荐版本">\u2661</button>';
  const uncensoredBadge = item.uncensored ? '<span class="badge uncensored">无码</span>' : "";
  const variantBadge = item.variant_count > 1
    ? '<span class="badge variants">' + item.variant_count + " 个语言版本</span>" : "";

  const variants = item.variant_count > 1
    ? '<details class="variants-list"><summary>查看 ' + item.variant_count + ' 个版本</summary><ul>' +
      item.variants.map((variant) =>
        '<li title="' + esc(variant.title_pretty || variant.title) + '">' +
        (variant.favorite ? '<span class="badge fav-mark">♥</span> ' : "") +
        '<span class="badge lang-' + esc(variant.language) + '">' + esc(variant.language_label) + "</span> " +
        '<a href="' + esc(variant.url) + '" target="_blank" rel="noopener noreferrer">#' + esc(variant.gallery_id) + "</a></li>"
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

  return '<article class="card">' +
    '<div class="thumb-wrap"><a href="' + esc(item.url) + '" target="_blank" rel="noopener noreferrer">' +
      localCover + "</a>" + favBtn + "</div>" +
    '<div class="card-body">' +
      '<h3 class="card-title"><a href="' + esc(item.url) + '" target="_blank" rel="noopener noreferrer">' + esc(item.title) + "</a></h3>" +
      originalLine +
      '<div class="badges">' + uncensoredBadge + variantBadge + langBadges + "</div>" +
      '<div class="badges">' + tagBadges + more + "</div>" +
      variants +
      '<div class="card-meta"><span>#' + esc(item.gallery_id) + "</span><span>" + esc(factLine) + "</span></div>" +
    "</div></article>";
}

function pageButton(pageNo, current) {
  return '<button class="page-btn' + (pageNo === current ? " current" : "") + '" data-page="' + pageNo + '">' + pageNo + "</button>";
}

function renderPager(data) {
  const pager = $("pager");
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

/* ---------------- 抓取 ---------------- */
async function startScrape() {
  const url = $("urlInput").value.trim();
  if (!url) { toast("请先填写列表页 URL", "error"); return; }
  const maxPages = Number($("maxPages").value || 0);
  const delay = Number($("delay").value || 1.2);
  state.finishedRunId = null;
  $("startBtn").disabled = true;
  $("progressPanel").hidden = false;
  try {
    await api("/api/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: url,
        max_pages: maxPages,
        delay: delay,
        enrich: $("enrichCheck").checked,
        covers: $("coverCheck").checked,
      }),
    });
    toast("已开始抓取，进度会实时更新");
    startPolling();
  } catch (error) {
    $("startBtn").disabled = false;
    toast(error.message, "error");
  }
}

function startPolling() {
  stopPolling();
  state.polling = window.setInterval(pollStatus, 1000);
  pollStatus();
}

function stopPolling() {
  if (state.polling) {
    window.clearInterval(state.polling);
    state.polling = null;
  }
}

async function pollStatus() {
  let status;
  try {
    status = await api("/api/scrape/status");
  } catch (error) {
    stopPolling();
    $("startBtn").disabled = false;
    return;
  }
  renderProgress(status);
  $("startBtn").disabled = Boolean(status.running);
  $("cancelBtn").disabled = !status.running;

  if (!status.running && ["done", "cancelled", "error"].includes(status.status) && state.finishedRunId !== status.run_id) {
    state.finishedRunId = status.run_id;
    if (status.status === "done") {
      const coverNote = status.covers_done ? "，封面 " + status.covers_done + " 张" : "";
      toast("抓取完成：条目 " + status.items_seen + "（新增 " + status.items_new + "，重复 " + status.items_dup + "）" + coverNote, "ok");
      refreshAll();
    } else if (status.status === "cancelled") {
      toast("抓取已取消（已抓取的数据已保存）");
      refreshAll();
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
  if (total > 0) {
    bar.classList.remove("indeterminate");
    bar.style.width = Math.min(100, Math.round((done / total) * 100)) + "%";
  } else if (running) {
    bar.classList.add("indeterminate");
    bar.style.width = "35%";
  } else {
    bar.classList.remove("indeterminate");
    bar.style.width = done > 0 ? "100%" : "0%";
  }
  const statusText = { idle: "空闲", running: "抓取中", done: "已完成", cancelled: "已取消", error: "失败" }[status.status] || status.status;
  const detailText = (status.details_total || status.details_done)
    ? " · 详情 " + (status.details_done || 0) + "/" + (status.details_total || 0)
      + ((status.details_failed || 0) ? "（失败 " + status.details_failed + "）" : "")
    : "";
  const coverText = (status.covers_done || status.covers_failed)
    ? " · 封面 " + (status.covers_done || 0) + " 张"
      + ((status.covers_failed || 0) ? "（失败 " + status.covers_failed + "）" : "")
    : "";
  $("progressText").innerHTML = "状态：<b>" + esc(statusText) + "</b> · 页 " + done + "/" + (total || "?") +
    " · 发现 " + (status.items_seen || 0) + " · 新增 " + (status.items_new || 0) + " · 重复 " + (status.items_dup || 0) +
    detailText + coverText + (status.error ? " · " + esc(status.error) : "");
  $("logBox").textContent = (status.logs || []).slice(-12).join("\n");
  $("cancelBtn").disabled = !running;
}

async function refreshAll() {
  await Promise.all([
    loadMeta().catch(() => {}),
    loadWorks().catch(() => {}),
  ]);
}

/* ---------------- 事件绑定 ---------------- */
let searchTimer = null;

function bindEvents() {
  $("grid").addEventListener("click", async (event) => {
    const btn = event.target.closest("button.fav-btn");
    if (!btn) return;
    event.preventDefault();
    const ids = (btn.dataset.favIds || "").split(",").map(Number).filter(Boolean);
    if (!ids.length) return;
    const wasFavorite = btn.classList.contains("active");
    btn.disabled = true;
    try {
      await api("/api/favorite", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: ids, favorite: !wasFavorite }),
      });
      toast(wasFavorite ? "已取消收藏" : "已收藏 \u2665", "ok");
      await loadWorks();
      loadMeta().catch(() => {});
    } catch (error) {
      toast(error.message, "error");
      btn.disabled = false;
    }
  });


  $("startBtn").addEventListener("click", startScrape);
  $("urlInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") startScrape();
  });
  $("cancelBtn").addEventListener("click", async () => {
    try {
      await api("/api/scrape/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      toast("已发送取消请求");
    } catch (error) {
      toast(error.message, "error");
    }
  });

  $("searchInput").addEventListener("input", (event) => {
    const value = event.target.value;
    if (searchTimer) window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.q = value.trim();
      state.page = 1;
      loadWorks().catch((error) => toast(error.message, "error"));
    }, 320);
  });

  $("dedupeCheck").addEventListener("change", (event) => {
    state.dedupe = event.target.checked;
    state.page = 1;
    loadWorks().catch((error) => toast(error.message, "error"));
  });

  $("favoriteCheck").addEventListener("change", (event) => {
    state.favoriteOnly = event.target.checked;
    state.page = 1;
    loadWorks().catch((error) => toast(error.message, "error"));
  });

  $("uncensoredCheck").addEventListener("change", (event) => {
    state.uncensored = event.target.checked;
    state.page = 1;
    loadWorks().catch((error) => toast(error.message, "error"));
  });

  $("sortSelect").addEventListener("change", (event) => {
    state.sort = event.target.value;
    state.page = 1;
    loadWorks().catch((error) => toast(error.message, "error"));
  });

  $("perPageSelect").addEventListener("change", (event) => {
    state.perPage = Number(event.target.value) || 48;
    state.page = 1;
    loadWorks().catch((error) => toast(error.message, "error"));
  });

  $("exportBtn").addEventListener("click", () => {
    window.location.href = "/api/export.csv?" + buildParams(false).toString();
  });

  $("resetBtn").addEventListener("click", async () => {
    if (!window.confirm("确定清空本地 SQLite 中的所有作品数据与抓取记录吗？此操作不可撤销。")) return;
    try {
      await api("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      toast("已清空本地数据", "ok");
      state.page = 1;
      await refreshAll();
    } catch (error) {
      toast(error.message, "error");
    }
  });

  $("langBar").addEventListener("click", (event) => {
    const button = event.target.closest("button.chip");
    if (!button) return;
    const key = button.dataset.lang || "";
    if (!key) {
      state.languages = [];
    } else if (state.languages.includes(key)) {
      state.languages = state.languages.filter((item) => item !== key);
    } else {
      state.languages = state.languages.concat(key);
    }
    state.page = 1;
    loadWorks().catch((error) => toast(error.message, "error"));
  });

  $("pager").addEventListener("click", (event) => {
    const button = event.target.closest("button.page-btn");
    if (!button || button.disabled) return;
    const pageNo = Number(button.dataset.page);
    if (!pageNo || pageNo < 1) return;
    state.page = pageNo;
    loadWorks().catch((error) => toast(error.message, "error"));
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

(async function init() {
  bindEvents();
  try {
    await loadMeta();
    await loadWorks();
  } catch (error) {
    toast("加载失败：" + error.message, "error");
  }
})();