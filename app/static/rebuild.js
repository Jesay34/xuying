let rebuildChannels = [];
const rebuildFlash = document.getElementById("rebuild-flash");

function rebuildMessage(text, kind = "info") {
  rebuildFlash.hidden = false;
  rebuildFlash.className = `flash ${kind}`;
  rebuildFlash.textContent = text;
}

function isoDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function selectedDates() {
  if (document.getElementById("date-mode").value === "range") {
    return {
      start: document.getElementById("rebuild-start").value,
      end: document.getElementById("rebuild-end").value,
    };
  }
  const [year, month] = document.getElementById("rebuild-month").value
    .split("-").map(Number);
  const first = new Date(year, month - 1, 1);
  const last = new Date(year, month, 0);
  return { start: isoDate(first), end: isoDate(last) };
}

function applyRebuildChannel(chatId) {
  const channel = rebuildChannels.find(
    (item) => String(item.chat_id) === String(chatId)
  );
  if (!channel) return;
  document.getElementById("rebuild-channel-name").value = channel.name || String(channel.chat_id);
  document.getElementById("rebuild-chat-id").value = channel.chat_id;
  document.getElementById("grouping-mode").value = channel.grouping_mode || "telegram_album";
  document.getElementById("marker-text").value = channel.marker_text || "1";
  document.getElementById("advertisement-policy").value = channel.advertisement_policy || "quarantine";
  document.getElementById("display-spacing-hours").value = String(channel.display_spacing_hours ?? 24);
  document.getElementById("timeline-mode").value = channel.timeline_mode || "album";
  document.getElementById("max-concurrent-downloads").value = String(channel.max_concurrent_downloads ?? 5);
}

async function loadRebuildChannels() {
  const selector = document.getElementById("channel-preset");
  const submit = document.querySelector("#rebuild-form button[type='submit']");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 8000);
  submit.disabled = true;
  selector.innerHTML = '<option value="">正在读取已配置频道…</option>';
  try {
    const response = await fetch(`/api/settings/configured-channels?_=${Date.now()}`, {
      cache: "no-store",
      signal: controller.signal,
    });
    const state = await response.json();
    if (!response.ok) throw new Error(state.detail || "读取频道配置失败");
    rebuildChannels = (state.channels || []).filter((channel) => channel.enabled !== false);
    selector.innerHTML = "";
    for (const channel of rebuildChannels) {
      const option = document.createElement("option");
      option.value = String(channel.chat_id);
      option.textContent = `${channel.name || channel.chat_id} · ${channel.chat_id}`;
      selector.appendChild(option);
    }
    if (!rebuildChannels.length) {
      selector.innerHTML = '<option value="">请先到“频道”页面添加监听频道</option>';
      rebuildMessage("历史补全只使用已配置频道。请先到“频道”页面添加频道。", "error");
      return;
    }
    applyRebuildChannel(selector.value);
    submit.disabled = false;
  } catch (error) {
    selector.innerHTML = '<option value="">频道读取失败，请点“刷新列表”</option>';
    rebuildMessage(
      error.name === "AbortError" ? "读取频道超时，请点击“刷新列表”重试。" : error.message,
      "error",
    );
  } finally {
    window.clearTimeout(timeout);
  }
}

document.getElementById("channel-preset").addEventListener("change", (event) => {
  applyRebuildChannel(event.target.value);
});
document.getElementById("refresh-rebuild-channels").addEventListener("click", loadRebuildChannels);
document.getElementById("manage-rebuild-channels").addEventListener("click", () => {
  window.location.href = "/setup#channel-step";
});

document.getElementById("date-mode").addEventListener("change", (event) => {
  const range = event.target.value === "range";
  document.getElementById("month-field").hidden = range;
  document.getElementById("start-field").hidden = !range;
  document.getElementById("end-field").hidden = !range;
  document.getElementById("rebuild-month").required = !range;
  document.getElementById("rebuild-start").required = range;
  document.getElementById("rebuild-end").required = range;
});

const now = new Date();
document.getElementById("rebuild-month").value =
  `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
loadRebuildChannels();

document.getElementById("rebuild-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  const dates = selectedDates();
  if (!dates.start || !dates.end) {
    rebuildMessage("请选择完整的日期范围", "error");
    return;
  }
  button.disabled = true;
  button.textContent = "正在创建…";
  try {
    const response = await fetch("/api/rebuild/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: Number(document.getElementById("rebuild-chat-id").value),
        channel_name: document.getElementById("rebuild-channel-name").value.trim(),
        start_date: dates.start,
        end_date: dates.end,
        grouping_mode: document.getElementById("grouping-mode").value,
        marker_text: document.getElementById("marker-text").value.trim() || "1",
        advertisement_policy: document.getElementById("advertisement-policy").value,
        display_spacing_hours: Number(document.getElementById("display-spacing-hours").value),
        timeline_mode: document.getElementById("timeline-mode").value,
        generate_xmp: document.getElementById("generate-xmp").checked,
        max_concurrent_downloads: Number(document.getElementById("max-concurrent-downloads").value),
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "任务创建失败");
    rebuildMessage(`任务 T${body.id} 已创建，将在后台重新下载。`, "success");
    document.getElementById("safe-confirm").checked = false;
    await loadRebuildTasks();
  } catch (error) {
    rebuildMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "创建历史重构任务";
  }
});

function escapeRebuild(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[character]);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 3);
  return `${(bytes / (1024 ** index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function formatDuration(value, status = "") {
  if (status === "completed") return "已完成";
  if (status === "paused") return "已暂停";
  if (status === "failed") return "已停止";
  if (status === "cancelled") return "已取消";
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return "计算中";
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分钟`;
  return `${(seconds / 3600).toFixed(1)} 小时`;
}

function rebuildPhaseIndex(task) {
  if (task.status === "completed") return 4;
  if (task.status === "organizing") return 3;
  if (task.status === "running") return 2;
  if (task.status === "scanning") return 1;
  return 0;
}

function renderRebuildPhases(task) {
  const current = rebuildPhaseIndex(task);
  const phases = ["读取", "下载", "整理", "完成"];
  return `<div class="task-phase-rail">${phases.map((label, index) => `
    <span class="${index + 1 < current ? "done" : index + 1 === current ? "current" : ""}">
      <i>${index + 1 < current ? "✓" : index + 1}</i><small>${label}</small>
    </span>`).join("")}</div>`;
}

function renderRebuildActiveFiles(task) {
  const active = task.active_downloads || [];
  if (!active.length) return "";
  return `<div class="active-download-list rebuild-active-list">
    <p class="section-caption">正在并发下载 ${active.length} 个文件</p>
    ${active.map((item) => {
      const received = Number(item.received || 0);
      const total = Number(item.total || 0);
      const percent = total ? Math.min(100, received * 100 / total) : 0;
      return `<article class="active-file">
        <div class="active-file-heading">
          <span>${escapeRebuild(item.filename || `消息 ${item.message_id || ""}`)}</span>
          <small>${formatBytes(item.speed_bps)}/s</small>
        </div>
        <div class="mini-progress"><i style="width:${percent}%"></i></div>
        <small>${formatBytes(received)}${total ? ` / ${formatBytes(total)}` : ""}</small>
      </article>`;
    }).join("")}
  </div>`;
}

async function taskAction(taskId, action) {
  const prompts = {
    cancel: "取消这个任务？已下载媒体会保留，下次仍可复用。",
    delete: "清除这条任务提示？不会删除原始媒体和整理库。",
    "repair-order": "重新读取原档并安全校正排序？不会重新下载，也不会移动已经进入 Immich 的媒体路径。",
  };
  if (prompts[action] && !window.confirm(prompts[action])) return;
  try {
    const suffix = action === "delete" ? "" : `/${action}`;
    const response = await fetch(`/api/rebuild/tasks/${taskId}${suffix}`, {
      method: action === "delete" ? "DELETE" : "POST",
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "操作失败");
    rebuildMessage(body.message || "操作完成", "success");
    await loadRebuildTasks();
  } catch (error) {
    rebuildMessage(error.message, "error");
  }
}

async function loadRebuildTasks() {
  const response = await fetch("/api/rebuild/tasks");
  const tasks = await response.json();
  const list = document.getElementById("rebuild-list");
  if (!tasks.length) {
    list.innerHTML = '<p class="empty">尚未创建历史重构任务。</p>';
    return;
  }
  const labels = {
    queued: "等待开始",
    scanning: "正在统计",
    running: "正在下载",
    organizing: "正在整理",
    paused: "已暂停",
    cancelled: "已取消",
    completed: "重构完成",
    failed: "任务失败",
  };
  const statusLabel = (task) => {
    if (task.status === "running" && task.phase === "rate_limited") {
      return "Telegram 限速等待";
    }
    if (task.status === "running" && task.phase === "retrying") {
      return "连接重试中";
    }
    return labels[task.status] || task.status;
  };
  list.innerHTML = tasks.map((task) => `
    <article class="rebuild-task task-${escapeRebuild(task.status)}">
      <div class="task-heading">
        <div>
          <strong>T${String(task.display_id || task.id).padStart(6, "0")} · ${escapeRebuild(task.channel_name)}</strong>
          <small>${escapeRebuild(task.start_date)} 至 ${escapeRebuild(task.end_date)}</small>
        </div>
        <span class="badge status-${escapeRebuild(task.status)}">${escapeRebuild(statusLabel(task))}</span>
      </div>
      ${renderRebuildPhases(task)}
      <div class="progress-track"
           ${["queued", "scanning", "running", "organizing"].includes(task.status)
             ? `data-task="${task.id}" data-action="pause" role="button" title="点击暂停"`
             : task.status === "paused"
               ? `data-task="${task.id}" data-action="resume" role="button" title="点击继续"`
               : ""}>
        <div class="progress-fill" style="width:${Math.max(0, Math.min(100, Number(task.percent || 0)))}%"></div>
      </div>
      <div class="progress-summary">
        <strong>${task.total ? `${task.progress || 0} / ${task.total}` : `${task.progress || 0} 个媒体`}</strong>
        <span>${task.total ? `${task.percent || 0}%` : "正在读取总数"}</span>
      </div>
      <div class="task-metrics task-metric-grid">
        <span><small>待处理</small><strong>${task.pending || 0}</strong></span>
        <span><small>新下载</small><strong>${task.new_downloads || 0}</strong></span>
        <span><small>跳过重复</small><strong>${task.reused || 0}</strong></span>
        <span><small>当前速度</small><strong>${["completed", "failed", "cancelled"].includes(task.status) ? "—" : `${formatBytes(task.speed_bps)}/s`}</strong></span>
        <span><small>并发任务</small><strong>${task.active_download_count || 0} / ${task.max_concurrent_downloads || 1}</strong></span>
        <span><small>预计剩余</small><strong>${formatDuration(task.eta_seconds, task.status)}</strong></span>
        <span><small>资源组</small><strong>${task.groups || 0}</strong></span>
        <span><small>人物批次</small><strong>${task.subjects || 0}</strong></span>
        <span><small>广告组</small><strong>${task.advertisements || 0}</strong></span>
      </div>
      ${renderRebuildActiveFiles(task)}
      <div class="task-actions">
        ${["queued", "scanning", "running", "organizing"].includes(task.status)
          ? `<button type="button" data-task="${task.id}" data-action="pause">暂停</button>
             <button type="button" class="danger-button" data-task="${task.id}" data-action="cancel">取消</button>`
          : ""}
        ${["paused", "failed", "cancelled"].includes(task.status)
          ? `<button type="button" class="primary-compact" data-task="${task.id}" data-action="resume">继续</button>`
          : ""}
        ${task.status === "completed"
          ? `<button type="button" class="primary-compact" data-task="${task.id}" data-action="repair-order">安全校正排序</button>`
          : ""}
        ${["completed", "failed", "cancelled", "paused"].includes(task.status)
          ? `<button type="button" class="danger-button" data-task="${task.id}" data-action="delete">清除记录</button>`
          : ""}
      </div>
      ${task.retry_reason
        ? `<p class="task-notice">${escapeRebuild(task.retry_reason)}${
            task.cooldown_until
              ? `，约 ${Math.max(0, Number(task.cooldown_until) - Math.floor(Date.now() / 1000))} 秒后继续`
              : ""
          }</p>`
        : ""}
      ${task.coordinated_with_live
        ? `<p class="task-coordination">已自动协调实时监听：重复媒体会复用，完成后统一重算人物相册边界。</p>`
        : ""}
      ${task.library_path
        ? `<code class="task-path">${escapeRebuild(task.library_path)}</code>`
        : ""}
      ${task.error
        ? `<p class="task-error">${escapeRebuild(task.error)}</p>`
        : ""}
    </article>
  `).join("");
  list.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () =>
      taskAction(Number(button.dataset.task), button.dataset.action)
    );
  });
}

document.getElementById("rebuild-refresh").addEventListener("click", loadRebuildTasks);
document.getElementById("timeline-mode").addEventListener("change", (event) => {
  document.getElementById("spacing-field").hidden = event.target.value !== "spaced";
});
document.getElementById("spacing-field").hidden = true;
loadRebuildTasks();
setInterval(loadRebuildTasks, 5000);
