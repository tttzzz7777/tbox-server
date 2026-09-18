// tbox-server WebUI — vanilla JS, no build step.
// Polls /admin/terminals every 5s and renders detail panes on click.

const API = {
  async get(path) {
    const r = await fetch(path, { headers: { accept: "application/json" } });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status} on ${path}: ${txt.slice(0, 200)}`);
    }
    return await r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(data.message || `HTTP ${r.status} on ${path}`);
    }
    return data;
  },
};

// ---- State --------------------------------------------------------------
let terminals = [];
let selectedTid = null;
let detailCache = { tid: null, status: null, uploads: null, history: null, lastError: null };
// Tracks which terminal the command form was built for. The form DOM
// (and any user input inside it) is preserved across auto-refreshes;
// only rebuilt when the user picks a different terminal.
let formMountedForTid = null;

const POLL_MS = 10000;
let lastRefreshTs = 0;
let pollInFlight = false;

// ---- Utilities ----------------------------------------------------------
function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const now = Date.now() / 1000;
  const diff = now - ts;
  if (diff < 60) return `${Math.floor(diff)}s 前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`;
  return d.toLocaleString();
}
function fmtBytes(n) {
  if (n == null) return "-";
  const f = Number(n);
  for (const u of ["B", "KiB", "MiB", "GiB"]) {
    if (f < 1024) return `${f.toFixed(1)} ${u}`;
  }
  return `${f.toFixed(1)} TiB`;
}
function fmtTs(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString();
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, kind = "") {
  const el = document.getElementById("toast");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

// ---- Data loaders -------------------------------------------------------
async function loadHealth() {
  try {
    const h = await API.get("/healthz");
    const dot = document.getElementById("health-dot");
    const txt = document.getElementById("health-text");
    if (h.status === "ok") {
      dot.className = "status-dot ok";
      txt.textContent = "服务正常";
    } else {
      dot.className = "status-dot stale";
      txt.textContent = h.status || "异常";
    }
    document.getElementById("server-version").textContent = `v${h.server_version || "?"}`;
  } catch (e) {
    document.getElementById("health-dot").className = "status-dot stale";
    document.getElementById("health-text").textContent = "不可达";
  }
}

async function loadTerminals() {
  try {
    const body = await API.get("/admin/terminals");
    terminals = body.terminals || [];
    renderTerminalList();
    if (selectedTid) {
      // Refresh detail if the selected terminal still exists.
      const found = terminals.find((t) => t.terminal_id === selectedTid);
      if (!found) {
        selectedTid = null;
        renderDetailEmpty();
      }
    }
  } catch (e) {
    toast(`加载终端列表失败：${e.message}`, "err");
  }
}

async function loadDetail(tid, opts = { rebuildForm: true }) {
  const isNewSelection = selectedTid !== tid;
  detailCache = { tid, status: null, uploads: null, history: null, lastError: null };

  if (isNewSelection || opts.rebuildForm) {
    renderDetailSkeleton(tid);
    formMountedForTid = null;
  }

  const tasks = [
    ["report", API.get(`/admin/report?terminal_id=${encodeURIComponent(tid)}&type=status`)
      .then((b) => (detailCache.status = b.payload)).catch(() => {})],
    ["uploads", API.get(`/admin/uploads?terminal_id=${encodeURIComponent(tid)}`)
      .then((b) => (detailCache.uploads = b.uploads || [])).catch(() => {})],
    ["history", API.get(`/admin/history?terminal_id=${encodeURIComponent(tid)}`)
      .then((b) => (detailCache.history = b.history || [])).catch(() => {})],
  ];
  // `tasks` is an array of [name, promise] tuples. Promise.all only awaits
  // thenables, so we have to unwrap the tuples — otherwise it sees plain
  // arrays and resolves immediately, before the fetches complete (the
  // status pane then renders with detailCache.status still null).
  await Promise.all(tasks.map(([, p]) => p));

  // Refresh only the data sections; preserve the form DOM (and any
  // user input) across auto-refresh ticks.
  renderDetailHeader(tid);
  renderDetailData(tid);
  if (formMountedForTid !== tid) {
    mountPushForm(tid);
    formMountedForTid = tid;
  }
}

// ---- Auto-refresh --------------------------------------------------------

function pulseRefreshButton() {
  const btn = document.getElementById("refresh-btn");
  if (!btn) return;
  btn.classList.add("pulsing");
  setTimeout(() => btn.classList.remove("pulsing"), 400);
}

function updateRefreshLabel() {
  const el = document.getElementById("refresh-label");
  if (!el) return;
  if (!lastRefreshTs) {
    el.textContent = "—";
    return;
  }
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - lastRefreshTs));
  el.textContent = `${elapsed}s 前`;
}

async function refreshAll(opts = { silent: false }) {
  // Avoid stacking: if a previous round is still running, skip this tick.
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    await loadHealth();
    await loadTerminals();
    if (selectedTid) {
      // Only re-fetch detail if it's still in the terminal list.
      // Pass rebuildForm:false so auto-refresh keeps the push form (and
      // any user input — e.g. a partially filled "upload" payload) intact.
      if (terminals.find((t) => t.terminal_id === selectedTid)) {
        await loadDetail(selectedTid, { rebuildForm: false });
      }
    }
    lastRefreshTs = Date.now() / 1000;
    if (!opts.silent) pulseRefreshButton();
    updateRefreshLabel();
  } catch (e) {
    toast(`自动刷新失败：${e.message}`, "err");
  } finally {
    pollInFlight = false;
  }
}

// ---- Renderers ----------------------------------------------------------
// ---- terminal sidebar: collapse state + helpers ------------------------
const LS_OFFLINE_OPEN = "tbox.sidebar.collapse.offline";
function loadOfflineOpen() {
  try { return localStorage.getItem(LS_OFFLINE_OPEN) === "true"; }
  catch (_) { return false; }
}
function saveOfflineOpen(open) {
  try { localStorage.setItem(LS_OFFLINE_OPEN, open ? "true" : "false"); }
  catch (_) {}
}

// Track offline group's open state at module scope. We don't read
// localStorage on every re-render because that races with the
// <details> toggle event (which is what would have just written
// the new value) and produces a briefly-collapsed group right
// after the user clicks a card inside it.
let offlineOpen = loadOfflineOpen();

function renderCardHtml(t) {
  const sel = t.terminal_id === selectedTid ? "selected" : "";
  const dotCls = t.online ? "ok" : "offline";
  const pending = t.pending_commands || 0;
  const pcls = pending > 0 ? "" : "zero";
  // Offline cards get a "×" close button (right side). The button only
  // removes the in-memory session — reports/uploads on disk are kept,
  // and a later heartbeat from the same terminal_id re-registers it as
  // a fresh session.
  const closeBtn = t.online
    ? ""
    : `<button class="close-btn" data-action="close" title="关闭（仅移除内存中的 session，磁盘文件保留）" aria-label="关闭">×</button>`;
  return `
      <div class="terminal-card ${sel}" data-tid="${esc(t.terminal_id)}">
        <div class="row1">
          <span class="tid"><span class="status-dot ${dotCls}"></span> ${esc(t.terminal_id)}</span>
          <span class="row1-right">
            <span class="pending ${pcls}">${pending} 待下发</span>
            ${closeBtn}
          </span>
        </div>
        <div class="row2">
          <span>${esc(t.service || "-")}</span>
          <span>${fmtTime(t.last_seen)}</span>
        </div>
      </div>`;
}

function renderGroupHtml(group, label, list, open) {
  if (!list.length) return "";
  const openAttr = open ? " open" : "";
  return `
    <details class="term-group" data-group="${group}"${openAttr}>
      <summary class="term-group-summary">
        <span class="term-group-caret" aria-hidden="true">▸</span>
        <span class="term-group-label">${label}</span>
        <span class="term-group-count muted">(${list.length})</span>
      </summary>
      <div class="term-group-body">
        ${list.map(renderCardHtml).join("")}
      </div>
    </details>`;
}

// One-time event delegation on the sidebar so auto-refresh re-renders
// (every 10s) don't need to re-bind handlers.
//
// Card clicks deliberately do NOT call renderTerminalList(): rebuilding
// the <details> via innerHTML on every selection was causing the offline
// group to collapse (the innerHTML replacement and the click event
// finishing its bubble through the freshly-created <details> race in a
// way that visibly snapped the group shut). Instead we just flip the
// .selected class on the affected cards; the next auto-refresh will
// rebuild the whole list with the correct open state from `offlineOpen`.
function setupTerminalListDelegation() {
  const wrap = document.getElementById("terminals");
  if (!wrap || wrap.dataset.delegated === "1") return;
  wrap.dataset.delegated = "1";
  wrap.addEventListener("click", (e) => {
    // 0. Close button on an offline card → confirm + API + refresh.
    //    Must run before the .terminal-card handler below, otherwise the
    //    click would bubble up and re-select the card we're about to
    //    remove (and cancel the visual state the user just saw).
    const closeEl = e.target.closest('[data-action="close"]');
    if (closeEl && wrap.contains(closeEl)) {
      e.stopPropagation();
      const card = closeEl.closest(".terminal-card");
      if (!card) return;
      const tid = card.dataset.tid;
      const ok = window.confirm(
        `确定关闭终端 ${tid} 吗？\n` +
        `关闭后只从内存中移除该 session；磁盘上的 reports/uploads 文件不会被删除。\n` +
        `（如果终端又发心跳，会作为新 session 重新出现）`
      );
      if (!ok) return;
      closeTerminal(tid);
      return;
    }
    // 1. Card click → select terminal. Update the .selected class in
    //    place; don't touch the surrounding <details>.
    const card = e.target.closest(".terminal-card");
    if (card && wrap.contains(card)) {
      selectedTid = card.dataset.tid;
      wrap.querySelectorAll(".terminal-card").forEach((el) => {
        el.classList.toggle("selected", el.dataset.tid === selectedTid);
      });
      loadDetail(selectedTid);
      return;
    }
    // 2. Summary click on the offline group → persist the new state.
    //    The browser has already toggled details.open by the time our
    //    click handler runs, so reading it back is reliable.
    const summary = e.target.closest(".term-group-summary");
    if (summary && wrap.contains(summary)) {
      const det = summary.parentElement;
      if (!(det instanceof HTMLDetailsElement)) return;
      if (det.dataset.group !== "offline") return;
      offlineOpen = det.open;
      saveOfflineOpen(offlineOpen);
    }
  });
}

// Close an offline terminal: POST to the backend, toast the result, and
// always re-pull the list so the UI doesn't drift (e.g. the card stays
// visible if the server rejected the close for any reason).
async function closeTerminal(tid) {
  try {
    await API.post("/admin/terminal/close", { terminal_id: tid });
    toast(`已关闭 ${tid}`, "ok");
  } catch (err) {
    toast(`关闭失败：${err.message}`, "err");
  } finally {
    loadTerminals();
  }
}

function renderTerminalList() {
  const wrap = document.getElementById("terminals");
  document.getElementById("term-count").textContent = `(${terminals.length})`;
  if (!terminals.length) {
    wrap.innerHTML = `<p class="empty">暂无终端</p>`;
    return;
  }
  // Sort: online first, then by last_seen desc. After splitting into
  // online/offline groups the intra-group order is preserved.
  terminals.sort((a, b) => {
    if (a.online !== b.online) return a.online ? -1 : 1;
    return (b.last_seen || 0) - (a.last_seen || 0);
  });
  const onlineList = terminals.filter((t) => t.online);
  const offlineList = terminals.filter((t) => !t.online);
  wrap.innerHTML =
    renderGroupHtml("online", "在线", onlineList, true) +
    renderGroupHtml("offline", "离线", offlineList, offlineOpen);
}

function renderDetailEmpty() {
  formMountedForTid = null;
  document.getElementById("detail-panel").innerHTML = `
    <div class="empty">
      <p>← 从左侧选择一个终端</p>
      <p class="muted small">支持查看状态、下发命令、上传触发、查看历史</p>
    </div>`;
}

function renderDetailSkeleton(tid) {
  document.getElementById("detail-panel").innerHTML = `
    <h2 class="detail-title"><span class="status-dot unknown"></span> ${esc(tid)}</h2>
    <p class="subtitle">加载中…</p>
    <div class="panes" id="detail-panes">
      <div class="pane"><h3>终端状态</h3><p class="muted">…</p></div>
      <div class="pane"><h3>最新 status 上报</h3><p class="muted">…</p></div>
      <div class="pane"><h3>已上传文件</h3><p class="muted">…</p></div>
      <div class="pane"><h3>ack 历史</h3><p class="muted">…</p></div>
    </div>
    <div id="detail-form-mount"></div>`;
}

function renderDetailHeader(tid) {
  const sess = terminals.find((t) => t.terminal_id === tid);
  const dotCls = sess?.online ? "ok" : "offline";
  const titleEl = document.querySelector("#detail-panel .detail-title");
  if (titleEl) {
    titleEl.innerHTML = `<span class="status-dot ${dotCls}"></span> ${esc(tid)}`;
  }
  const subEl = document.querySelector("#detail-panel .subtitle");
  if (subEl) {
    subEl.textContent = sess
      ? `${sess.online ? "在线" : "离线"} · 上次心跳 ${fmtTime(sess.last_seen)} · 待下发 ${sess.pending_commands || 0}`
      : "终端已下线 / 未注册";
  }
}

function renderDetailData(tid) {
  const sess = terminals.find((t) => t.terminal_id === tid);
  const wrap = document.getElementById("detail-panes");
  if (!wrap) return;
  wrap.innerHTML = [
    renderSessionPane(sess),
    renderStatusPane(detailCache.status),
    renderUploadsPane(detailCache.uploads),
    renderHistoryPane(detailCache.history),
  ].join("");
}

function mountPushForm(tid) {
  const mount = document.getElementById("detail-form-mount");
  if (!mount) return;
  mount.innerHTML = renderPushPane(tid);
  setupCommandForm();
}

function renderSessionPane(sess) {
  if (!sess) return `<div class="pane"><h3>终端状态</h3><p class="muted">终端已下线 / 未注册</p></div>`;
  const items = [
    ["terminal_id", sess.terminal_id, true],
    ["service", sess.service || "—"],
    ["online", sess.online ? "是" : "否"],
    ["last_seen", fmtTime(sess.last_seen) + (sess.last_seen ? ` (${fmtTs(sess.last_seen)})` : "")],
    ["pending_commands", sess.pending_commands || 0],
    ["last_report", sess.last_report ? `${sess.last_report.type} @ ${fmtTime(sess.last_report.ts)}` : "—"],
  ];
  return `
    <div class="pane">
      <h3>终端状态</h3>
      <div class="info-grid">
        ${items.map(([k, v, mono]) => `
          <div class="item">
            <span class="k">${esc(k)}</span>
            <span class="v ${mono ? "" : "mono"}">${esc(v)}</span>
          </div>`).join("")}
      </div>
    </div>`;
}

function renderStatusPane(status) {
  let body;
  if (!status) {
    body = `<p class="muted">尚未收到 status 上报</p>`;
  } else {
    const pos = status.position || {};
    const dtc = Array.isArray(status.dtc) ? status.dtc : [];
    body = `
      <div class="info-grid">
        <div class="item"><span class="k">iccid</span><span class="v">${esc(status.iccid || "—")}</span></div>
        <div class="item"><span class="k">imei</span><span class="v">${esc(status.imei || "—")}</span></div>
        <div class="item"><span class="k">imsi</span><span class="v">${esc(status.imsi || "—")}</span></div>
        <div class="item"><span class="k">vin</span><span class="v">${esc(status.vin || "—")}</span></div>
        <div class="item"><span class="k">sn</span><span class="v">${esc(status.sn || "—")}</span></div>
        <div class="item"><span class="k">vehicle_model</span><span class="v">${esc(status.vehicle_model || "—")}</span></div>
      </div>
      <h3 style="margin-top:12px;font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.04em">position</h3>
      <div class="info-grid">
        <div class="item"><span class="k">lat</span><span class="v">${esc(pos.lat ?? "—")}</span></div>
        <div class="item"><span class="k">lon</span><span class="v">${esc(pos.lon ?? "—")}</span></div>
        <div class="item"><span class="k">alt</span><span class="v">${esc(pos.alt ?? "—")}</span></div>
        <div class="item"><span class="k">speed</span><span class="v">${esc(pos.speed ?? "—")}</span></div>
        <div class="item"><span class="k">track</span><span class="v">${esc(pos.track ?? "—")}</span></div>
        <div class="item"><span class="k">status/mode</span><span class="v">${esc(pos.status ?? "—")}/${esc(pos.mode ?? "—")}</span></div>
      </div>
      ${dtc.length ? `
        <h3 style="margin-top:12px;font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.04em">dtc</h3>
        <div class="dt-list">${dtc.map((c) => `<span>${esc(c)}</span>`).join("")}</div>
      ` : ""}
      <details style="margin-top:10px"><summary class="muted small">查看原始 JSON</summary>
        <pre class="json">${esc(JSON.stringify(status, null, 2))}</pre>
      </details>`;
  }
  return `<div class="pane"><h3>最新 status 上报</h3>${body}</div>`;
}

function renderUploadsPane(list) {
  if (!list) {
    return `<div class="pane"><h3>已上传文件</h3><p class="muted">加载失败</p></div>`;
  }
  if (!list.length) {
    return `<div class="pane"><h3>已上传文件</h3><p class="muted">暂无上传</p></div>`;
  }
  return `
    <div class="pane">
      <h3>已上传文件 (${list.length})</h3>
      <table class="simple">
        <thead>
          <tr><th>日期</th><th>文件名</th><th>大小</th><th>cmd_id</th><th>上传时间</th></tr>
        </thead>
        <tbody>
          ${list.map((u) => `
            <tr>
              <td class="mono">${esc(u.date_dir || "-")}</td>
              <td class="mono" title="${esc(u.path || "")}">${esc(u.filename || "-")}</td>
              <td>${esc(fmtBytes(u.size))}</td>
              <td class="mono">${esc((u.cmd_id || "").slice(0, 16))}${(u.cmd_id || "").length > 16 ? "…" : ""}</td>
              <td>${esc(fmtTs(u.mtime))}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

function renderHistoryPane(list) {
  if (!list) {
    return `<div class="pane"><h3>ack 历史</h3><p class="muted">加载失败</p></div>`;
  }
  if (!list.length) {
    return `<div class="pane"><h3>ack 历史</h3><p class="muted">暂无 ack</p></div>`;
  }
  return `
    <div class="pane">
      <h3>ack 历史 (最近 ${list.length})</h3>
      <table class="simple">
        <thead>
          <tr><th>cmd_id</th><th>状态</th><th>result</th><th>时间</th></tr>
        </thead>
        <tbody>
          ${list.map((h) => `
            <tr>
              <td class="mono">${esc((h.cmd_id || "").slice(0, 16))}${(h.cmd_id || "").length > 16 ? "…" : ""}</td>
              <td><span class="badge ${h.status === "ok" ? "ok" : "fail"}">${esc(h.status)}</span></td>
              <td><code>${esc(JSON.stringify(h.result || {}))}</code></td>
              <td>${esc(fmtTs(h.ack_ts))}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

const DEFAULT_EXEC = { command: "uptime", timeout: 30 };

function nowLocalDatetime() {
  // Format current local time as YYYY-MM-DDTHH:MM (datetime-local value).
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Convert a datetime-local value ("2026-09-17T18:30") to the protocol
// format ("2026-09-17 18:30"). Returns "" for empty input.
function dtLocalToProtocol(s) {
  return s ? s.replace("T", " ") : "";
}

function renderPushPane(tid) {
  return `
    <div class="pane">
      <h3>下发命令</h3>
      <form class="push-form" id="push-form" data-tid="${esc(tid)}">
        <div class="row">
          <label for="cmd-type">类型</label>
          <select id="cmd-type">
            <option value="exec">exec — 在终端执行 shell 命令</option>
            <option value="upload">upload — 让终端上传文件</option>
          </select>
        </div>

        <!-- ===== exec fields ===== -->
        <div class="cmd-fields" data-for="exec">
          <div class="row">
            <label for="exec-command">命令</label>
            <input type="text" id="exec-command" autocomplete="off"
                   placeholder="例如：ls -la /var/log" value="${esc(DEFAULT_EXEC.command)}">
          </div>
          <div class="row">
            <label for="exec-timeout">超时（秒）</label>
            <input type="number" id="exec-timeout" min="1" max="3600"
                   value="${esc(DEFAULT_EXEC.timeout)}">
          </div>
        </div>

        <!-- ===== upload fields ===== -->
        <div class="cmd-fields hidden" data-for="upload">
          <div class="row">
            <label for="upload-kind">上传什么</label>
            <select id="upload-kind">
              <option value="log">日志（按时间窗口抓 messages）</option>
              <option value="other">指定文件</option>
            </select>
          </div>

          <div class="upload-fields" data-for="log">
            <div class="row">
              <label for="log-start">起始时间</label>
              <div class="input-group">
                <input type="datetime-local" id="log-start">
                <button type="button" class="btn small"
                        data-action="now" data-target="log-start">现在</button>
                <button type="button" class="btn small"
                        data-action="clear" data-target="log-start">清空</button>
              </div>
            </div>
            <div class="row">
              <label for="log-end">结束时间</label>
              <div class="input-group">
                <input type="datetime-local" id="log-end">
                <button type="button" class="btn small"
                        data-action="now" data-target="log-end">现在</button>
                <button type="button" class="btn small"
                        data-action="clear" data-target="log-end">清空</button>
              </div>
            </div>
            <p class="hint">起始或结束留空 = 不限。终端会按窗口抓 messages 并打包上传。</p>
          </div>

          <div class="upload-fields hidden" data-for="other">
            <div class="row">
              <label for="upload-path">文件路径</label>
              <input type="text" id="upload-path" autocomplete="off"
                     placeholder="例如：/data/dump.bin">
            </div>
            <p class="hint">终端会读取这个绝对路径的文件，原样 POST 到服务器。</p>
          </div>
        </div>

        <div class="row actions">
          <label></label>
          <div class="actions-row">
            <button type="submit" class="btn primary">下发</button>
            <button type="button" class="btn" id="cmd-reset-btn">重置</button>
            <span class="hint">返回 cmd_id；终端 poll 后会执行</span>
          </div>
        </div>

        <details class="payload-preview">
          <summary class="muted small">查看将发送的 JSON（高级）</summary>
          <pre class="json" id="payload-preview">{}</pre>
        </details>
      </form>
    </div>`;
}

function buildPayload() {
  const type = document.getElementById("cmd-type").value;
  if (type === "exec") {
    const command = document.getElementById("exec-command").value.trim();
    const timeout = Number(document.getElementById("exec-timeout").value) || 30;
    return { type, payload: { command, timeout } };
  }
  // upload
  const kind = document.getElementById("upload-kind").value;
  if (kind === "log") {
    const payload = { file_type: "log" };
    const s = document.getElementById("log-start").value;
    const e = document.getElementById("log-end").value;
    const sStr = dtLocalToProtocol(s);
    const eStr = dtLocalToProtocol(e);
    if (sStr) payload.start_time = sStr;
    if (eStr) payload.end_time = eStr;
    return { type, payload };
  }
  // other
  const path = document.getElementById("upload-path").value.trim();
  return { type, payload: { file_type: "other", path } };
}

function refreshPayloadPreview() {
  const el = document.getElementById("payload-preview");
  if (!el) return;
  try {
    el.textContent = JSON.stringify(buildPayload().payload, null, 2);
  } catch (e) {
    el.textContent = "(unable to build preview)";
  }
}

function setupCommandForm() {
  const form = document.getElementById("push-form");
  if (!form) return;

  // ---- fieldset switching ------------------------------------------------
  function showCmdFields(forType) {
    form.querySelectorAll(".cmd-fields").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.for !== forType);
    });
  }
  function showUploadFields(forKind) {
    form.querySelectorAll(".upload-fields").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.for !== forKind);
    });
  }

  const typeSel = document.getElementById("cmd-type");
  const uploadKindSel = document.getElementById("upload-kind");

  typeSel.addEventListener("change", () => {
    showCmdFields(typeSel.value);
    refreshPayloadPreview();
  });
  uploadKindSel.addEventListener("change", () => {
    showUploadFields(uploadKindSel.value);
    refreshPayloadPreview();
  });

  // ---- now / clear buttons ----------------------------------------------
  form.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset.target);
      if (!target) return;
      if (btn.dataset.action === "now") target.value = nowLocalDatetime();
      else if (btn.dataset.action === "clear") target.value = "";
      refreshPayloadPreview();
    });
  });

  // ---- keep JSON preview in sync ----------------------------------------
  form.querySelectorAll("input, select").forEach((el) => {
    el.addEventListener("input", refreshPayloadPreview);
    el.addEventListener("change", refreshPayloadPreview);
  });

  // ---- reset button ------------------------------------------------------
  document.getElementById("cmd-reset-btn").addEventListener("click", () => {
    document.getElementById("exec-command").value = DEFAULT_EXEC.command;
    document.getElementById("exec-timeout").value = DEFAULT_EXEC.timeout;
    document.getElementById("upload-path").value = "";
    document.getElementById("log-start").value = "";
    document.getElementById("log-end").value = "";
    typeSel.value = "exec";
    uploadKindSel.value = "log";
    showCmdFields("exec");
    showUploadFields("log");
    refreshPayloadPreview();
  });

  // ---- submit ------------------------------------------------------------
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const tid = form.dataset.tid;
    const { type, payload } = buildPayload();

    // Client-side checks so users see friendly errors before the request.
    if (type === "exec") {
      if (!payload.command) {
        toast("请输入要执行的命令", "err");
        return;
      }
      if (!(payload.timeout > 0)) {
        toast("超时必须是正数", "err");
        return;
      }
    } else if (type === "upload") {
      if (payload.file_type === "other" && !payload.path) {
        toast("请输入要上传的文件路径", "err");
        return;
      }
    }

    try {
      const res = await API.post("/admin/command", { terminal_id: tid, type, payload });
      toast(`已下发 ${type} → ${tid}（cmd_id=${res.cmd_id}）`, "ok");
      loadTerminals();
    } catch (err) {
      toast(`下发失败：${err.message}`, "err");
    }
  });

  // Initial state.
  showCmdFields(typeSel.value);
  showUploadFields(uploadKindSel.value);
  refreshPayloadPreview();
}

// ---- Init ---------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  setupTerminalListDelegation();
  document.getElementById("refresh-btn").addEventListener("click", () => {
    // Manual refresh: same path as auto, but forces the button pulse.
    refreshAll({ silent: false });
  });

  // First paint, then start the auto-refresh loop.
  refreshAll({ silent: true });
  setInterval(refreshAll, POLL_MS);
  // Tick the "X s 前" label every second so the countdown feels live.
  setInterval(updateRefreshLabel, 1000);
});
