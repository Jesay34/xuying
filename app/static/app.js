async function getJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function text(id, value) {
  document.getElementById(id).textContent = value;
}

function applyServiceStatus(status) {
  const serviceLabels = {
    running: "正在监听",
    connection_error: "网络中断",
    login_required: "等待登录",
    disabled: "监听未启用",
  };
  const offline = status.telegram === "connection_error";
  const serviceLabel = serviceLabels[status.telegram] || "服务正常";
  const accelerationLabel = status.download_acceleration === "cryptg"
    ? "C 加速"
    : "兼容模式";
  text("system-status", `${serviceLabel} · ${accelerationLabel}`);
  document.getElementById("status-dot").style.background = offline
    ? "#f2777a"
    : "#77e1b5";
  document.getElementById("safe-notice").hidden = !status.safe_mode;
  renderNetworkNotice(status);
}

async function loadServiceStatus() {
  applyServiceStatus(await getJson("/api/status"));
}

async function loadDashboard() {
  const [status, stats, groups] = await Promise.all([
    getJson("/api/status"),
    getJson("/api/stats"),
    getJson("/api/groups"),
  ]);
  applyServiceStatus(status);
  text("messages", stats.messages);
  text("media-files", stats.media_files);
  text("groups", stats.groups);
  text("organized", stats.organized_groups);

  const list = document.getElementById("groups-list");
  if (!groups.length) return;
  const statusLabels = {
    pending: "等待整理",
    organizing: "正在整理",
    organized: "已整理",
    error: "整理失败",
    open: "等待下一位人物",
    excluded: "仅保留原始下载",
  };
  list.innerHTML = groups.map((group) => `
    <article class="group">
      <div>
        <strong>${escapeHtml(group.public_id)}</strong>
        <small>消息 ${group.start_message_id}–${group.end_message_id} · ${group.file_count} 项</small>
      </div>
      <span class="reason badge">${escapeHtml(group.reason)}</span>
      <span class="badge">${Math.round(group.confidence * 100)}% · ${escapeHtml(statusLabels[group.status] || group.status)}</span>
      ${["pending", "error"].includes(group.status)
        ? `<button type="button" class="organize-button" data-group-id="${group.id}">立即整理</button>`
        : ""}
    </article>
  `).join("");
  list.querySelectorAll(".organize-button").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "整理中…";
      try {
        await getJson(`/api/groups/${button.dataset.groupId}/organize`, { method: "POST" });
        await loadDashboard();
      } catch (error) {
        button.disabled = false;
        button.textContent = "重试整理";
        window.alert(`整理失败：${error.message}`);
      }
    });
  });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[character]);
}

function formatLiveBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 3);
  return `${(bytes / (1024 ** index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function renderNetworkNotice(status) {
  const notice = document.getElementById("network-notice");
  if (!notice) return;
  const listenerDown = status.telegram === "connection_error";
  const botDown = Boolean(status.bot_enabled && status.bot_error);
  if (!listenerDown && !botDown) {
    notice.hidden = true;
    notice.innerHTML = "";
    return;
  }
  const lines = [];
  if (listenerDown) {
    lines.push(`频道监听已断开：${escapeHtml(status.telegram_error || "网络或代理无响应")}`);
  }
  if (botDown) {
    lines.push(`转发机器人已断开：${escapeHtml(status.bot_error)}`);
    lines.push("未完成的转发下载已退回队列，机器人重连后会自动继续，也可以在任务里手动继续。");
  }
  const scope = listenerDown && botDown
    ? "频道监听与转发机器人"
    : listenerDown ? "频道监听" : "转发机器人";
  notice.innerHTML = `
    <strong>网络或代理异常：${scope}离线</strong>
    ${lines.map((line) => `<small>${line}</small>`).join("")}
    <small>序影每 60 秒自动检测一次，网络恢复后会自动重连，无需重启项目。</small>
  `;
  notice.hidden = false;
}

function renderLiveDownloads(state) {
  const center = document.getElementById("live-download-center");
  const badge = document.getElementById("live-status-badge");
  const labels = {
    running: "正在监听",
    paused: "下载已暂停",
    disabled: "监听未启用",
    login_required: "等待登录",
    connection_error: "连接异常",
  };
  badge.textContent = labels[state.status] || state.status || "服务正常";
  badge.className = `badge status-${state.paused ? "paused" : state.status || "running"}`;

  const totalConcurrency = (state.channels || [])
    .filter((channel) => channel.enabled)
    .reduce((sum, channel) => sum + Number(channel.max_concurrent_downloads || 0), 0);
  const active = state.active_downloads || [];
  center.innerHTML = `
    <div class="live-summary-grid">
      <article><small>等待下载</small><strong>${state.queued || 0}</strong><em>新资源自动进入队列</em></article>
      <article><small>正在下载</small><strong>${state.downloading || 0}</strong><em>并发 ${state.downloading || 0} / ${totalConcurrency || 0}</em></article>
      <article><small>当前速度</small><strong>${formatLiveBytes(state.speed_bps)}/s</strong><em>本次 ${formatLiveBytes(state.session_bytes)}</em></article>
      <article><small>失败待重试</small><strong>${state.failed || 0}</strong><em>继续时自动重试</em></article>
    </div>
    <div class="live-toolbar">
      <div>
        <strong>${state.paused ? "队列已安全暂停" : "自动下载与整理运行中"}</strong>
        <small>${state.pending || 0} 个待处理，已完成 ${state.completed || 0} 个</small>
      </div>
      <div class="task-actions">
        <button type="button" class="${state.paused ? "primary-compact" : ""}"
                id="live-toggle">${state.paused ? "继续下载" : "暂停下载"}</button>
        <a class="button-link subtle-button" href="/setup">调整频道并发</a>
      </div>
    </div>
    ${(state.channels || []).length ? `
      <div class="live-channel-grid">
        ${(state.channels || []).map((channel) => `
          <article>
            <div><strong>${escapeHtml(channel.name)}</strong><small>${channel.enabled ? "监听中" : "已停用"}</small></div>
            <div class="channel-queue-values">
              <span><b>${channel.queued || 0}</b> 排队</span>
              <span><b>${channel.downloading || 0}</b> 下载</span>
              <span><b>${channel.max_concurrent_downloads || 5}</b> 并发</span>
            </div>
          </article>`).join("")}
      </div>` : ""}
    ${active.length ? `
      <div class="active-download-list">
        <p class="section-caption">当前文件</p>
        ${active.map((item) => {
          const total = Number(item.total || 0);
          const received = Number(item.received || 0);
          const percent = total ? Math.min(100, received * 100 / total) : 0;
          return `
            <article class="active-file">
              <div class="active-file-heading">
                <span>${escapeHtml(item.filename)}</span>
                <small>${formatLiveBytes(item.speed_bps)}/s</small>
              </div>
              <div class="mini-progress"><i style="width:${percent}%"></i></div>
              <small>${formatLiveBytes(received)}${total ? ` / ${formatLiveBytes(total)}` : ""}</small>
            </article>`;
        }).join("")}
      </div>` : ""}
  `;
  document.getElementById("live-toggle").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    const action = state.paused ? "resume" : "pause";
    try {
      await getJson(`/api/downloads/live/${action}`, { method: "POST" });
      await loadLiveDownloads();
    } catch (error) {
      window.alert(`操作失败：${error.message}`);
      event.currentTarget.disabled = false;
    }
  });
}

async function loadLiveDownloads() {
  const state = await getJson("/api/downloads/live/status");
  renderLiveDownloads(state);
}

document.getElementById("refresh").addEventListener("click", () => loadDashboard());
loadDashboard().catch((error) => {
  text("system-status", "连接异常");
  console.error(error);
});
loadLiveDownloads().catch((error) => console.error(error));
setInterval(loadLiveDownloads, 2500);
setInterval(() => loadServiceStatus().catch((error) => console.error(error)), 15000);
