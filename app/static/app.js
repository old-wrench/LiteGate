"use strict";
/* LiteGate 面板交互 —— 原生 JS，无任何构建依赖 */

var $ = function (s) { return document.querySelector(s); };

function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== null && text !== undefined) n.textContent = text;
  return n;
}

async function api(path, opts) {
  var resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new Error("网络错误：" + e.message);
  }
  if (!resp.ok) {
    var msg = "HTTP " + resp.status;
    try {
      var j = await resp.json();
      if (j && j.detail) msg = j.detail;
      else if (j && j.error && j.error.message) msg = j.error.message;
    } catch (_) {}
    throw new Error(msg);
  }
  return resp.json();
}

function toast(msg, isErr) {
  var box = $("#toasts");
  var t = el("div", "toast" + (isErr ? " err" : ""), String(msg));
  box.appendChild(t);
  setTimeout(function () {
    t.style.opacity = "0";
    t.style.transition = "opacity .3s";
    setTimeout(function () { t.remove(); }, 320);
  }, isErr ? 5200 : 3000);
}

function fmtN(n) { return Number(n || 0).toLocaleString("en-US"); }

/* 每轮/分组缓存命中率：cached ÷ prompt；输入为0时无法计算，显示 — */
function hitRate(cached, prompt) {
  prompt = Number(prompt); cached = Number(cached || 0);
  return prompt > 0 ? (cached / prompt * 100).toFixed(1) + "%" : "\u2014";
}

function fmtTs(sec) {
  var d = new Date(Number(sec) * 1000);
  function p(x) { return String(x).padStart(2, "0"); }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function pad2(x) { return String(x).padStart(2, "0"); }

function localDT(d) {
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
    "T" + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

function numOrNull(id) {
  var v = $(id).value.trim();
  if (!v) return null;
  var n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/* =======================================================================
 * Tab 切换
 * ===================================================================== */
document.querySelectorAll(".tab").forEach(function (btn) {
  btn.addEventListener("click", function () {
    document.querySelectorAll(".tab").forEach(function (b) { b.classList.remove("active"); });
    btn.classList.add("active");
    var id = "page-" + btn.dataset.tab;
    document.querySelectorAll(".page").forEach(function (p) { p.classList.remove("active"); });
    $("#" + id).classList.add("active");
    if (btn.dataset.tab === "stats") loadStatsPage();
  });
});

/* =======================================================================
 * 虚拟 Key 分发管理
 * ===================================================================== */
function maskKey(k) {
  k = k || "";
  return k ? ("••••••••" + k.slice(-4)) : "—";
}

/* 复制到剪贴板：http 非安全上下文里 navigator.clipboard 不存在（还会同步抛错），
   统一走"API优先 + execCommand 降级"，成败都只给 toast，不弹任何确认框 */
function legacyCopy(text) {
  try {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    var ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}
function copyText(text) {
  return new Promise(function (resolve, reject) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(resolve, function () {
        legacyCopy(text) ? resolve() : reject(new Error("execCommand 失败"));
      });
    } else {
      legacyCopy(text) ? resolve() : reject(new Error("Clipboard API 不可用"));
    }
  });
}

async function loadListen() {
  var c = await api("/admin/config");
  $("#set-listen").value = c.listen_addr || "";
  var acc = c.admin_access || {};
  $("#set-access-mode").value = acc.mode || "lan";
  $("#set-access-allow").value = (acc.allow || []).join("\n");
  $("#set-access-allow").disabled = $("#set-access-mode").value !== "allowlist";
}

async function refreshKeys() {
  try {
    var list = await api("/admin/api_keys");
    renderKeys(list);
  } catch (e) { toast(e.message, true); }
}

function renderKeys(list) {
  var tb = $("#keys-body");
  tb.innerHTML = "";
  $("#key-count").textContent = list.length;
  list.forEach(function (k) {
    var tr = document.createElement("tr");

    var tdName = document.createElement("td");
    tdName.appendChild(el("b", "cell-name", k.name));
    if (!k.enabled) tdName.appendChild(el("span", "chip", " 停用 "));
    tr.appendChild(tdName);

    var tdKey = el("td", "mono key-mask", maskKey(k.key));
    tdKey.title = "点击「显示」查看完整值";
    tr.appendChild(tdKey);

    var tdSt = document.createElement("td");
    tdSt.appendChild(el("span", k.enabled ? "chip on" : "chip",
      k.enabled ? "启用中" : "已停用"));
    tr.appendChild(tdSt);

    var tdOps = el("td", "ops");

    var bShow = el("button", "btn sm", "显示");
    bShow.type = "button";
    bShow.addEventListener("click", function () {
      var on = bShow.textContent === "隐藏";
      tdKey.textContent = on ? maskKey(k.key) : k.key;
      tdKey.className = "mono " + (on ? "key-mask" : "key-full");
      bShow.textContent = on ? "显示" : "隐藏";
    });
    tdOps.appendChild(bShow);

    var bCopy = el("button", "btn sm", "复制");
    bCopy.type = "button";
    bCopy.addEventListener("click", function () {
      copyText(k.key).then(
        function () { toast("已复制「" + k.name + "」的Key，可直接发给同事"); },
        function () { toast("自动复制失败，请点「显示」后手动复制", true); }
      );
    });
    tdOps.appendChild(bCopy);

    var bToggle = el("button", "btn sm", k.enabled ? "停用" : "启用");
    bToggle.type = "button";
    bToggle.addEventListener("click", async function () {
      try {
        await api("/admin/api_keys/" + encodeURIComponent(k.id), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !k.enabled })
        });
        toast((k.enabled ? "已停用：" : "已启用：") + k.name);
        refreshKeys();
      } catch (e) { toast(e.message, true); }
    });
    tdOps.appendChild(bToggle);

    var bEdit = el("button", "btn sm", "编辑");
    bEdit.type = "button";
    bEdit.addEventListener("click", function () { openKeyModal(k); });
    tdOps.appendChild(bEdit);

    var bDel = el("button", "btn sm danger", "删除");
    bDel.type = "button";
    bDel.addEventListener("click", async function () {
      if (!window.confirm("删除 Key「" + k.name + "」？\n该同事将立即无法调用，历史统计保留。")) return;
      try {
        var r = await api("/admin/api_keys/" + encodeURIComponent(k.id), { method: "DELETE" });
        if (r.warn_no_enabled_keys) toast("警告：当前没有任何启用的Key，网关将拒绝所有请求！", true);
        else toast("已删除：" + k.name);
        refreshKeys();
        refreshMeta();
      } catch (e) { toast(e.message, true); }
    });
    tdOps.appendChild(bDel);

    tr.appendChild(tdOps);
    tb.appendChild(tr);
  });
}

$("#btn-new-key").addEventListener("click", function () { openKeyModal(null); });

function openKeyModal(item) {
  var isNew = !item;
  $("#key-modal-title").textContent = isNew ? "新增虚拟Key" : "编辑虚拟Key：" + item.name;
  $("#kf-id").value = isNew ? "" : item.id;
  $("#kf-name").value = isNew ? "" : item.name;
  $("#kf-key").value = isNew ? "" : item.key;
  $("#kf-enabled").checked = isNew ? true : !!item.enabled;
  $("#kf-show").checked = false;
  $("#kf-key").type = "password";
  $("#key-modal").classList.add("show");
  setTimeout(function () { $("#kf-name").focus(); }, 40);
}
function closeKeyModal() { $("#key-modal").classList.remove("show"); }
$("#kmodal-x").addEventListener("click", closeKeyModal);
$("#kmodal-cancel").addEventListener("click", closeKeyModal);
$("#key-modal").addEventListener("click", function (ev) {
  if (ev.target === $("#key-modal")) closeKeyModal();
});
$("#kf-show").addEventListener("change", function () {
  $("#kf-key").type = this.checked ? "text" : "password";
});
$("#kf-gen").addEventListener("click", function () {
  var bytes = new Uint8Array(18);
  crypto.getRandomValues(bytes);
  var s = btoa(String.fromCharCode.apply(null, bytes))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  $("#kf-key").value = "sk-virtual-" + s;
  $("#kf-key").type = "text";
  $("#kf-show").checked = true;
});

$("#key-form").addEventListener("submit", async function (ev) {
  ev.preventDefault();
  var payload = {
    name: $("#kf-name").value.trim(),
    key: $("#kf-key").value.trim(),
    enabled: $("#kf-enabled").checked
  };
  var kid = $("#kf-id").value;
  try {
    if (kid) {
      await api("/admin/api_keys/" + encodeURIComponent(kid), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      toast("已保存Key：" + payload.name);
    } else {
      await api("/admin/api_keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      toast("已新增Key：" + payload.name + "（记得把Key发给同事）");
    }
    closeKeyModal();
    refreshKeys();
    refreshMeta();
  } catch (e) { toast(e.message, true); }
});

$("#btn-save-settings").addEventListener("click", async function () {
  try {
    var r = await api("/admin/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ listen_addr: $("#set-listen").value.trim() })
    });
    $("#set-listen").value = r.listen_addr;
    toast(r.listen_requires_restart ? "已保存。监听地址变更需重启服务生效" : "已保存");
  } catch (e) { toast(e.message, true); }
});

$("#set-access-mode").addEventListener("change", function () {
  $("#set-access-allow").disabled = this.value !== "allowlist";
});

$("#btn-save-access").addEventListener("click", async function () {
  var allow = $("#set-access-allow").value.split(/\r?\n/)
    .map(function (s) { return s.trim(); }).filter(Boolean);
  try {
    await api("/admin/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        admin_access: { mode: $("#set-access-mode").value, allow: allow }
      })
    });
    toast("访问控制已保存，约2秒内生效（本机 127.0.0.1 不受影响）");
  } catch (e) { toast(e.message, true); }
});

/* =======================================================================
 * 上游渠道 CRUD
 * ===================================================================== */
function renderUpstreams(list) {
  var tb = $("#upstreams-body");
  tb.innerHTML = "";
  $("#up-count").textContent = list.length;
  $("#up-empty").hidden = list.length > 0;
  list.forEach(function (u) {
    var tr = document.createElement("tr");
    var tdAlias = document.createElement("td");
    tdAlias.appendChild(el("b", "cell-name mono", u.alias));
    tr.appendChild(tdAlias);

    tr.appendChild(el("td", "mono dim-text", u.real_model));

    var tdBase = el("td", "mono clip dim-text", u.api_base);
    tdBase.title = u.api_base;
    tr.appendChild(tdBase);

    tr.appendChild(el("td", "mono dim-text", maskKey(u.api_key)));

    var tdTag = document.createElement("td");
    if (u.tag) tdTag.appendChild(el("span", "chip tag", u.tag));
    else { tdTag.className = "dim-text"; tdTag.textContent = "—"; }
    tr.appendChild(tdTag);
    [["budget", u.thinking_budget], ["maxtok", u.max_tokens], ["maxctx", u.max_context_tokens]]
      .forEach(function (p) {
        tr.appendChild(el("td", "num" + (p[1] == null ? " dim0" : ""),
          p[1] == null ? "—" : fmtN(p[1])));
      });

    var tdForce = document.createElement("td");
    if (u.force_override_client_params) {
      tdForce.appendChild(el("span", "chip on", "强制覆盖"));
    } else {
      tdForce.appendChild(el("span", "chip off", "客户端优先"));
    }
    tr.appendChild(tdForce);

    var tdPsu = document.createElement("td");
    if (u.parse_stream_usage) {
      tdPsu.appendChild(el("span", "chip on", "采集"));
    } else {
      tdPsu.appendChild(el("span", "chip off", "直通"));
    }
    tr.appendChild(tdPsu);

    var tdOps = el("td", "ops");
    var bEdit = el("button", "btn sm", "编辑");
    bEdit.type = "button";
    bEdit.addEventListener("click", function () { openModal(u); });
    var bDel = el("button", "btn sm danger", "删除");
    bDel.type = "button";
    bDel.addEventListener("click", async function () {
      if (!window.confirm("删除上游「" + u.alias + "」？\n相关的统计日志会保留。")) return;
      try {
        await api("/admin/upstreams/" + encodeURIComponent(u.id), { method: "DELETE" });
        toast("已删除：" + u.alias);
        refreshUpstreams();
        refreshMeta();
      } catch (e) { toast(e.message, true); }
    });
    tdOps.appendChild(bEdit);
    tdOps.appendChild(bDel);
    tr.appendChild(tdOps);
    tb.appendChild(tr);
  });
}

async function refreshUpstreams() {
  try {
    renderUpstreams(await api("/admin/upstreams"));
  } catch (e) { toast(e.message, true); }
}

$("#btn-refresh-upstreams").addEventListener("click", refreshUpstreams);

/* ---------- 上游弹窗 ---------- */
function openModal(item) {
  var isNew = !item;
  $("#modal-title").textContent = isNew ? "新增上游渠道" : "编辑上游渠道：" + item.alias;
  $("#f-id").value = isNew ? "" : item.id;
  $("#f-alias").value = isNew ? "" : item.alias;
  $("#f-real").value = isNew ? "" : item.real_model;
  $("#f-base").value = isNew ? "" : item.api_base;
  $("#f-key").value = isNew ? "" : (item.api_key || "");
  $("#f-tag").value = isNew ? "" : (item.tag || "");
  $("#f-budget").value = isNew || item.thinking_budget == null ? "" : item.thinking_budget;
  $("#f-maxtok").value = isNew || item.max_tokens == null ? "" : item.max_tokens;
  $("#f-maxctx").value = isNew || item.max_context_tokens == null ? "" : item.max_context_tokens;
  $("#f-force").checked = !isNew && !!item.force_override_client_params;
  $("#f-psu").checked = !isNew && !!item.parse_stream_usage;
  $("#f-key-show").checked = false;
  $("#f-key").type = "password";
  $("#modal").classList.add("show");
  setTimeout(function () { $("#f-alias").focus(); }, 40);
}
function closeModal() { $("#modal").classList.remove("show"); }
$("#btn-new").addEventListener("click", function () { openModal(null); });
$("#modal-x").addEventListener("click", closeModal);
$("#modal-cancel").addEventListener("click", closeModal);
$("#modal").addEventListener("click", function (ev) {
  if (ev.target === $("#modal")) closeModal();
});
$("#f-key-show").addEventListener("change", function () {
  $("#f-key").type = this.checked ? "text" : "password";
});

$("#up-form").addEventListener("submit", async function (ev) {
  ev.preventDefault();
  var payload = {
    alias: $("#f-alias").value.trim(),
    real_model: $("#f-real").value.trim(),
    api_base: $("#f-base").value.trim(),
    api_key: $("#f-key").value,
    tag: $("#f-tag").value.trim(),
    thinking_budget: numOrNull("#f-budget"),
    max_tokens: numOrNull("#f-maxtok"),
    max_context_tokens: numOrNull("#f-maxctx"),
    force_override_client_params: $("#f-force").checked,
    parse_stream_usage: $("#f-psu").checked
  };
  var uid = $("#f-id").value;
  try {
    if (uid) {
      await api("/admin/upstreams/" + encodeURIComponent(uid), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      toast("已保存：" + payload.alias);
    } else {
      await api("/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      toast("已新增：" + payload.alias);
    }
    closeModal();
    refreshUpstreams();
    refreshMeta();
  } catch (e) { toast(e.message, true); }
});

/* ---------- 导入 / 导出 ---------- */
$("#btn-import").addEventListener("click", function () { $("#import-file").click(); });
$("#import-file").addEventListener("change", function () {
  var file = this.files[0];
  this.value = "";
  if (!file) return;
  var reader = new FileReader();
  reader.onload = async function () {
    var content = String(reader.result || "");
    if (!window.confirm("导入将整体覆盖当前配置（旧配置自动备份为 .bak）。继续？")) return;
    try {
      var r = await api("/admin/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: content })
      });
      toast("导入成功：" + r.upstreams + " 条上游 / " + r.api_keys + " 把Key（原配置已备份 .bak）");
      refreshUpstreams();
      refreshKeys();
      loadListen().catch(function () {});
      refreshMeta();
    } catch (e) { toast("导入失败：" + e.message, true); }
  };
  reader.onerror = function () { toast("读取文件失败", true); };
  reader.readAsText(file, "utf-8");
});

/* =======================================================================
 * 统计看板
 * ===================================================================== */
var PAGE_SIZE = 200;
var pg = { offset: 0, total: 0 };

async function refreshMeta() {
  /* 筛选下拉以「当前配置」为准：编辑渠道/Key 后原选项被替换，
     而不是因历史日志残留出现新旧两条（旧别名的历史明细仍可在分组/明细中看到） */
  try {
    var keys = await api("/admin/api_keys");
    fillSelect($("#fl-client"), keys.map(function (k) { return k.name; }));
  } catch (e) { /* 面板局部失败不阻塞看板 */ }
  try {
    var ups = await api("/admin/upstreams");
    var tags = [], aliases = [];
    ups.forEach(function (u) {
      if (u.tag && tags.indexOf(u.tag) < 0) tags.push(u.tag);
      if (aliases.indexOf(u.alias) < 0) aliases.push(u.alias);
    });
    fillSelect($("#fl-tag"), tags);
    fillSelect($("#fl-alias"), aliases);
  } catch (e) { /* ignore */ }
}

function fillSelect(sel, items) {
  var cur = sel.value;
  sel.innerHTML = "";
  sel.appendChild(new Option("全部", ""));
  (items || []).forEach(function (v) { sel.appendChild(new Option(v, v)); });
  if ([].some.call(sel.options, function (o) { return o.value === cur; })) sel.value = cur;
}

function currentFilters() {
  function ts(v) {
    if (!v) return "";
    var t = Math.floor(new Date(v).getTime() / 1000);
    return Number.isFinite(t) ? String(t) : "";
  }
  return {
    start: ts($("#fl-start").value),
    end: ts($("#fl-end").value),
    client: $("#fl-client").value,
    tag: $("#fl-tag").value,
    alias: $("#fl-alias").value
  };
}

function qs(filters, extra) {
  var p = new URLSearchParams();
  Object.keys(filters).forEach(function (k) { if (filters[k]) p.set(k, filters[k]); });
  extra && Object.keys(extra).forEach(function (k) { if (extra[k] !== undefined) p.set(k, extra[k]); });
  var s = p.toString();
  return s ? "?" + s : "";
}

/* 大数缩写：1.2万 / 3.4亿，KPI 卡用；完整值见副标题 */
function fmtCompact(n) {
  n = Number(n || 0);
  var abs = Math.abs(n);
  if (abs >= 1e8) return (n / 1e8).toFixed(2) + "\u4ebf";
  if (abs >= 1e4) return (n / 1e4).toFixed(1) + "\u4e07";
  return fmtN(n);
}
/* 命中率配色：>=30% 绿 · >0 琥珀 · 无命中/无输入 灰 */
function rateClass(cached, prompt) {
  prompt = Number(prompt);
  cached = Number(cached || 0);
  if (!(prompt > 0)) return "rate-lo";
  var r = cached / prompt * 100;
  return r >= 30 ? "rate-hi" : (r > 0 ? "rate-mid" : "rate-lo");
}

/* 命中率 = cached ÷ prompt；分组同样用 SUM 相除而非平均 */
function drawSumTable(sel, rows, barCls) {
  var tb = $(sel);
  tb.innerHTML = "";
  if (!rows || !rows.length) {
    var tr0 = document.createElement("tr");
    var td0 = el("td", "dim-text", "暂无数据");
    td0.colSpan = 8;
    tr0.appendChild(td0);
    tb.appendChild(tr0);
    return;
  }
  var max = 0;
  rows.forEach(function (r) { if (Number(r.total) > max) max = Number(r.total); });
  rows.forEach(function (r) {
    var tr = document.createElement("tr");
    /* 分组名 + 占比条（相对该卡内最大合计） */
    var tdKey = document.createElement("td");
    var wrap = el("div", "cell-key");
    var k = el("span", "k mono", r.key);
    k.title = r.key;
    var bar = el("div", "bar " + (barCls || ""));
    var i = document.createElement("i");
    var t = Number(r.total) || 0;
    i.style.width = (t > 0 && max > 0 ? Math.max(2, Math.round(t / max * 100)) : 0) + "%";
    bar.appendChild(i);
    wrap.appendChild(k);
    wrap.appendChild(bar);
    tdKey.appendChild(wrap);
    tr.appendChild(tdKey);
    tr.appendChild(el("td", "num", fmtN(r.requests)));
    tr.appendChild(el("td", "num c-tools" + (Number(r.tools) ? "" : " dim0"), fmtN(r.tools)));
    tr.appendChild(el("td", "num c-in" + (Number(r.prompt) ? "" : " dim0"), fmtN(r.prompt)));
    tr.appendChild(el("td", "num c-out" + (Number(r.completion) ? "" : " dim0"), fmtN(r.completion)));
    tr.appendChild(el("td", "num c-cache" + (Number(r.cached || 0) ? "" : " dim0"), fmtN(r.cached || 0)));
    tr.appendChild(el("td", "num " + rateClass(r.cached, r.prompt), hitRate(r.cached, r.prompt)));
    tr.appendChild(el("td", "num c-total", fmtN(r.total)));
    tb.appendChild(tr);
  });
}

function renderSummary(s) {
  var g = s.grand || {};
  var kpis = [
    ["k1", "\u8bf7\u6c42\u6570", fmtN(g.requests), "\u6210\u529f\u8c03\u7528 \u00b7 \u5931\u8d25\u4e0d\u5165\u5e93"],
    ["k2", "\u5de5\u5177\u8c03\u7528", fmtN(g.tools), "\u54cd\u5e94\u4e2d\u7684 tool_calls \u6b21\u6570"],
    ["k3", "\u8f93\u5165 Tokens", fmtCompact(g.prompt), "\u5b8c\u6574\u503c " + fmtN(g.prompt)],
    ["k4", "\u8f93\u51fa Tokens", fmtCompact(g.completion), "\u5b8c\u6574\u503c " + fmtN(g.completion)],
    ["k5", "\u7f13\u5b58\u547d\u4e2d", fmtCompact(g.cached || 0), "\u547d\u4e2d\u7387 " + hitRate(g.cached, g.prompt)],
    ["k6", "\u5408\u8ba1 Tokens", fmtCompact(g.total), "\u8f93\u5165 + \u8f93\u51fa"]
  ];
  var grid = $("#kpi-grid");
  grid.innerHTML = "";
  kpis.forEach(function (p) {
    var c = el("div", "kpi " + p[0]);
    c.appendChild(el("div", "kpi-label", p[1]));
    c.appendChild(el("div", "kpi-value", p[2]));
    c.appendChild(el("div", "kpi-sub", p[3]));
    grid.appendChild(c);
  });
  var f = currentFilters();
  var parts = [];
  if (f.start || f.end) {
    parts.push("\u65f6\u95f4 " + (f.start ? new Date(f.start * 1000).toLocaleString() : "\u8d77\u70b9") +
      " ~ " + (f.end ? new Date(f.end * 1000).toLocaleString() : "\u73b0\u5728"));
  }
  if (f.client) parts.push("\u4f7f\u7528\u8005 " + f.client);
  if (f.tag) parts.push("Tag " + f.tag);
  if (f.alias) parts.push("\u522b\u540d " + f.alias);
  $("#kpi-caption").textContent =
    "\u7edf\u8ba1\u8303\u56f4\uff1a" + (parts.length ? parts.join(" \u00b7 ") : "\u5168\u90e8\u6570\u636e") +
    " \u00b7 \u66f4\u65b0\u4e8e " + new Date().toLocaleTimeString();
  drawSumTable("#sum-client", s.by_client, "b-client");
  drawSumTable("#sum-tag", s.by_tag, "b-tag");
  drawSumTable("#sum-alias", s.by_alias, "b-alias");
  drawSumTable("#sum-model", s.by_real_model, "b-model");
}

function renderLogs(data) {
  pg.total = data.total;
  var tb = $("#logs-body");
  tb.innerHTML = "";
  $("#logs-total").textContent = data.total;
  $("#logs-empty").hidden = data.rows.length > 0;

  data.rows.forEach(function (r) {
    var tr = document.createElement("tr");
    tr.appendChild(el("td", "mono dim-text", fmtTs(r.create_time)));
    tr.appendChild(el("td", "", r.client_name || "—"));
    tr.appendChild(el("td", "mono", r.alias));
    tr.appendChild(el("td", "mono", r.real_model));
    tr.appendChild(el("td", "", r.upstream_tag || "—"));
    tr.appendChild(el("td", "num c-in" + (Number(r.prompt_tokens) ? "" : " dim0"),
      fmtN(r.prompt_tokens)));
    tr.appendChild(el("td", "num c-out" + (Number(r.completion_tokens) ? "" : " dim0"),
      fmtN(r.completion_tokens)));
    tr.appendChild(el("td", "num c-cache" + (Number(r.cached_tokens) ? "" : " dim0"),
      fmtN(r.cached_tokens)));
    tr.appendChild(el("td", "num " + rateClass(r.cached_tokens, r.prompt_tokens),
      hitRate(r.cached_tokens, r.prompt_tokens)));
    tr.appendChild(el("td", "num c-tools" + (Number(r.tool_calls) ? "" : " dim0"),
      fmtN(r.tool_calls)));

    var tdNote = document.createElement("td");
    if (Number(r.is_stream) === 1 &&
        Number(r.prompt_tokens) === 0 && Number(r.completion_tokens) === 0) {
      var chip = el("span", "chip", "流式无usage");
      chip.title = "该渠道未开启流式采集或厂商不支持 stream_options，token 记为 0";
      tdNote.appendChild(chip);
    } else if (Number(r.is_stream) === 1) {
      var got = el("span", "chip on", "流式·已采集usage");
      got.title = "网关注入 stream_options.include_usage 后从 SSE 末片提取的真实用量";
      tdNote.appendChild(got);
    } else {
      tdNote.className = "dim-text";
      tdNote.textContent = "—";
    }
    tr.appendChild(tdNote);
    tb.appendChild(tr);
  });

  var from = data.total === 0 ? 0 : pg.offset + 1;
  var to = Math.min(pg.offset + data.limit, data.total);
  $("#pg-info").textContent = "第 " + from + "-" + to + " 条 / 共 " + data.total + " 条";
  $("#pg-prev").disabled = pg.offset <= 0;
  $("#pg-next").disabled = pg.offset + data.limit >= data.total;
}

async function queryStats(resetOffset) {
  if (resetOffset) pg.offset = 0;
  var f = currentFilters();
  try {
    var logsUrl = "/admin/stats/logs" + qs(f, { limit: PAGE_SIZE, offset: pg.offset });
    var sumUrl = "/admin/stats/summary" + qs(f);
    var results = await Promise.all([api(logsUrl), api(sumUrl)]);
    renderLogs(results[0]);
    renderSummary(results[1]);
  } catch (e) { toast(e.message, true); }
}

$("#btn-query").addEventListener("click", function () { queryStats(true); });
$("#pg-prev").addEventListener("click", function () {
  pg.offset = Math.max(0, pg.offset - PAGE_SIZE);
  queryStats(false);
});
$("#pg-next").addEventListener("click", function () {
  pg.offset = pg.offset + PAGE_SIZE;
  queryStats(false);
});

/* 快捷区间统一入口：同步写入开始/结束输入框并点亮对应按钮 */
function setQuickActive(r) {
  [].forEach.call(document.querySelectorAll("#quick-ranges .btn"), function (b) {
    b.classList.toggle("active", b.dataset.r === r);
  });
}
function applyQuickRange(r) {
  var now = new Date();
  var s = $("#fl-start"), e = $("#fl-end");
  var day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());        /* 今天 00:00 */
  var dayEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59); /* 今天 23:59 */
  switch (r) {
    case "today":
      s.value = localDT(day0); e.value = localDT(dayEnd); break;
    case "24h":
      s.value = localDT(new Date(now.getTime() - 864e5)); e.value = localDT(now); break;
    case "7d":
      s.value = localDT(new Date(day0.getTime() - 6 * 864e5)); e.value = localDT(dayEnd); break;
    case "30d":
      s.value = localDT(new Date(day0.getTime() - 29 * 864e5)); e.value = localDT(dayEnd); break;
    default:
      s.value = ""; e.value = "";
  }
  setQuickActive(r);
}
$("#quick-ranges").addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-r]");
  if (!b) return;
  applyQuickRange(b.dataset.r);
  queryStats(true);
});
/* 手动修改时间即视为自定义范围：取消快捷按钮高亮 */
["#fl-start", "#fl-end"].forEach(function (id) {
  $(id).addEventListener("input", function () { setQuickActive(""); });
});

$("#btn-reset-range").addEventListener("click", function () {
  ["#fl-start", "#fl-end"].forEach(function (id) { $(id).value = ""; });
  $("#fl-client").value = "";
  $("#fl-tag").value = "";
  $("#fl-alias").value = "";
  setQuickActive("");
  queryStats(true);
});

$("#btn-clear").addEventListener("click", async function () {
  if (!window.confirm("⚠ 将删除【全部】统计日志（含所有时间、所有渠道、所有使用者），此操作不可恢复！确定继续？")) return;
  if (!window.confirm("再次确认：真的要清空所有用量记录吗？")) return;
  try {
    var r = await api("/admin/stats/logs", { method: "DELETE" });
    toast("已清空 " + r.cleared + " 条统计日志");
    loadStatsPage();
  } catch (e) { toast(e.message, true); }
});

function loadStatsPage() {
  applyQuickRange("7d");   /* 默认查询最近一周：7天前00:00 ~ 今天23:59 */
  refreshMeta()
    .then(function () { return queryStats(true); })
    .catch(function (e) { toast(e.message, true); });
}

/* =======================================================================
 * 初始化
 * ===================================================================== */
(async function init() {
  refreshUpstreams();
  refreshKeys();
  loadListen().catch(function (e) { toast(e.message, true); });
})();
